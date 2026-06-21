"""All Bitbucket Cloud API HTTP calls — zero business logic, zero normalization.

httpx async client. Auth: Bearer access_token on `https://api.bitbucket.org/2.0`,
HTTP Basic (client_id:client_secret) on the OAuth token endpoint.

Retry policy (OCP — single source of truth):
- 429 / 5xx → exponential backoff with jitter, honours `Retry-After`
- transport errors → exponential backoff
- 401 → one-shot token refresh via the `token_refresh` callback, then replay
"""
import asyncio
import random
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
import structlog

from exceptions import (
    BitbucketAuthError,
    BitbucketBadRequestError,
    BitbucketConflictError,
    BitbucketError,
    BitbucketNetworkError,
    BitbucketNotFoundError,
    BitbucketRateLimitError,
    BitbucketServerError,
)

logger = structlog.get_logger(__name__)

_BITBUCKET_BASE = "https://api.bitbucket.org/2.0"

# Retry policy — single source of truth (OCP). Tests monkeypatch these to 0.
_MAX_RETRIES = 3
_BASE_DELAY_S = 1.0
_MAX_DELAY_S = 32.0
_BACKOFF_FACTOR = 2.0


class BitbucketHTTPClient:
    """Thin async httpx client for the Bitbucket Cloud REST API.

    All methods accept an *access_token* and return raw response dicts (or
    raw text for `get_file_content`). Auth + retry + refresh live here —
    the connector layer only orchestrates business calls.

    When a 401 is returned, the client invokes the `token_refresh` callback
    once to obtain a fresh access token and replays the request. If the
    second attempt also returns 401, `BitbucketAuthError` is raised.
    """

    def __init__(
        self,
        base_url: str = _BITBUCKET_BASE,
        token_refresh: Optional[Callable[[], Awaitable[str]]] = None,
        timeout_s: float = 30.0,
    ):
        self._base_url = (base_url or _BITBUCKET_BASE).rstrip("/")
        self._token_refresh = token_refresh
        self._timeout_s = timeout_s

    # ── Internals ──────────────────────────────────────────────────────────

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

    def _abs_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                message = err.get("message") or str(body)
            elif err is not None:
                message = str(err)
            else:
                message = body.get("message") or body.get("detail") or str(body)
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        body_arg = body if isinstance(body, dict) else {"raw": body}

        if status == 400:
            raise BitbucketBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body_arg,
            )
        if status in (401, 403):
            raise BitbucketAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_arg,
            )
        if status == 404:
            raise BitbucketNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_arg,
            )
        if status == 409:
            raise BitbucketConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_arg,
            )
        if status == 429:
            retry_after = self._parse_retry_after(response) or 5.0
            raise BitbucketRateLimitError(
                f"429 Rate limit exceeded{ctx}",
                status_code=429,
                response_body=body_arg,
                retry_after_s=retry_after,
            )
        if 500 <= status < 600:
            raise BitbucketServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_arg,
            )
        raise BitbucketError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_arg,
        )

    async def _request(
        self,
        method: str,
        url: str,
        access_token: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        context: str = "",
        _retry_after_refresh: bool = True,
    ) -> httpx.Response:
        """Single attempt + retry on 429/5xx + refresh-once on 401."""
        full_url = self._abs_url(url)
        headers = self._auth_headers(access_token) if access_token else {
            "Accept": "application/json"
        }

        last_exc: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    response = await client.request(
                        method,
                        full_url,
                        headers=headers,
                        params=params,
                        json=json_body,
                        data=data,
                    )
                except (
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.TransportError,
                ) as exc:
                    last_exc = exc
                    if attempt == _MAX_RETRIES:
                        raise BitbucketNetworkError(
                            f"Network error{': ' + context if context else ''}: {exc}"
                        ) from exc
                    await self._sleep_backoff(attempt)
                    continue

                # Refresh-on-401 (one-shot).
                if (
                    response.status_code == 401
                    and _retry_after_refresh
                    and self._token_refresh
                ):
                    try:
                        new_token = await self._token_refresh()
                    except Exception as exc:
                        raise BitbucketAuthError(
                            f"Token refresh failed: {exc}", status_code=401
                        ) from exc
                    return await self._request(
                        method,
                        url,
                        new_token,
                        params=params,
                        json_body=json_body,
                        data=data,
                        context=context,
                        _retry_after_refresh=False,
                    )

                # Retry on 429 / 5xx
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt == _MAX_RETRIES:
                        return response
                    retry_after = self._parse_retry_after(response)
                    logger.warning(
                        "bitbucket.http.retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        context=context,
                    )
                    await self._sleep_backoff(attempt, retry_after=retry_after)
                    continue

                return response

        if last_exc:
            raise BitbucketNetworkError(str(last_exc)) from last_exc
        raise BitbucketError("Request loop exited without response")

    async def _sleep_backoff(
        self,
        attempt: int,
        retry_after: Optional[float] = None,
    ) -> None:
        if retry_after is not None:
            delay = min(retry_after, _MAX_DELAY_S)
        else:
            delay = min(
                _BASE_DELAY_S * (_BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.3),
                _MAX_DELAY_S,
            )
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> Optional[float]:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    # ── Authentication ─────────────────────────────────────────────────────

    async def post_form_data(
        self,
        url: str,
        payload: Dict[str, str],
        context: str = "post_form_data",
        basic_auth: Optional[Tuple[str, str]] = None,
    ) -> Dict[str, Any]:
        """POST form-encoded data — used for OAuth token exchange/refresh.

        Bitbucket's token endpoint expects HTTP Basic auth (client_id:secret)
        plus a form-encoded body. *basic_auth* is an optional (id, secret) tuple.
        """
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            try:
                response = await client.post(
                    url,
                    data=payload,
                    auth=basic_auth,
                    headers={"Accept": "application/json"},
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise BitbucketNetworkError(
                    f"OAuth network error{': ' + context if context else ''}: {exc}"
                ) from exc
            await self._raise_for_status(response, context)
            try:
                return response.json()
            except Exception:
                return {"raw": response.text}

    # ── User ───────────────────────────────────────────────────────────────

    async def get_user(self, access_token: str) -> Dict[str, Any]:
        """GET /user — current authenticated user (used by health_check)."""
        response = await self._request(
            "GET", "/user", access_token, context="get_user"
        )
        await self._raise_for_status(response, "get_user")
        return response.json()

    # ── Workspaces ─────────────────────────────────────────────────────────

    async def list_workspaces(
        self,
        access_token: str,
        role: str = "member",
        pagelen: int = 50,
    ) -> Dict[str, Any]:
        """GET /workspaces."""
        params = {"role": role, "pagelen": pagelen}
        response = await self._request(
            "GET",
            "/workspaces",
            access_token,
            params=params,
            context="list_workspaces",
        )
        await self._raise_for_status(response, "list_workspaces")
        return response.json()

    async def get_workspace(
        self,
        access_token: str,
        workspace: str,
    ) -> Dict[str, Any]:
        """GET /workspaces/{workspace}."""
        response = await self._request(
            "GET",
            f"/workspaces/{workspace}",
            access_token,
            context=f"get_workspace({workspace})",
        )
        await self._raise_for_status(response, f"get_workspace({workspace})")
        return response.json()

    # ── Repositories ───────────────────────────────────────────────────────

    async def list_repositories(
        self,
        access_token: str,
        workspace: str,
        role: str = "member",
        pagelen: int = 50,
        page: int = 1,
    ) -> Dict[str, Any]:
        """GET /repositories/{workspace}."""
        params = {"role": role, "pagelen": pagelen, "page": page}
        response = await self._request(
            "GET",
            f"/repositories/{workspace}",
            access_token,
            params=params,
            context=f"list_repositories({workspace})",
        )
        await self._raise_for_status(response, f"list_repositories({workspace})")
        return response.json()

    async def get_repository(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
    ) -> Dict[str, Any]:
        """GET /repositories/{workspace}/{repo_slug}."""
        response = await self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}",
            access_token,
            context=f"get_repository({workspace}/{repo_slug})",
        )
        await self._raise_for_status(
            response, f"get_repository({workspace}/{repo_slug})"
        )
        return response.json()

    async def create_repository(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /repositories/{workspace}/{repo_slug}."""
        response = await self._request(
            "POST",
            f"/repositories/{workspace}/{repo_slug}",
            access_token,
            json_body=body,
            context=f"create_repository({workspace}/{repo_slug})",
        )
        await self._raise_for_status(
            response, f"create_repository({workspace}/{repo_slug})"
        )
        return response.json()

    async def delete_repository(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
    ) -> Dict[str, Any]:
        """DELETE /repositories/{workspace}/{repo_slug} — returns {} on 204."""
        response = await self._request(
            "DELETE",
            f"/repositories/{workspace}/{repo_slug}",
            access_token,
            context=f"delete_repository({workspace}/{repo_slug})",
        )
        await self._raise_for_status(
            response, f"delete_repository({workspace}/{repo_slug})"
        )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except Exception:
            return {}

    # ── Branches ───────────────────────────────────────────────────────────

    async def list_branches(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        pagelen: int = 50,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/refs/branches."""
        params = {"pagelen": pagelen}
        response = await self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}/refs/branches",
            access_token,
            params=params,
            context=f"list_branches({workspace}/{repo_slug})",
        )
        await self._raise_for_status(
            response, f"list_branches({workspace}/{repo_slug})"
        )
        return response.json()

    # ── Pull Requests ──────────────────────────────────────────────────────

    async def list_pull_requests(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        state: str = "OPEN",
        pagelen: int = 50,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/pullrequests."""
        params = {"state": state, "pagelen": pagelen}
        response = await self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}/pullrequests",
            access_token,
            params=params,
            context=f"list_pull_requests({workspace}/{repo_slug})",
        )
        await self._raise_for_status(
            response, f"list_pull_requests({workspace}/{repo_slug})"
        )
        return response.json()

    async def get_pull_request(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        pull_id: int,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/pullrequests/{id}."""
        response = await self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}/pullrequests/{pull_id}",
            access_token,
            context=f"get_pull_request({workspace}/{repo_slug}#{pull_id})",
        )
        await self._raise_for_status(
            response, f"get_pull_request({workspace}/{repo_slug}#{pull_id})"
        )
        return response.json()

    async def create_pull_request(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /repositories/{ws}/{slug}/pullrequests."""
        response = await self._request(
            "POST",
            f"/repositories/{workspace}/{repo_slug}/pullrequests",
            access_token,
            json_body=body,
            context=f"create_pull_request({workspace}/{repo_slug})",
        )
        await self._raise_for_status(
            response, f"create_pull_request({workspace}/{repo_slug})"
        )
        return response.json()

    async def merge_pull_request(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        pull_id: int,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /repositories/{ws}/{slug}/pullrequests/{id}/merge."""
        response = await self._request(
            "POST",
            f"/repositories/{workspace}/{repo_slug}/pullrequests/{pull_id}/merge",
            access_token,
            json_body=body,
            context=f"merge_pull_request({workspace}/{repo_slug}#{pull_id})",
        )
        await self._raise_for_status(
            response, f"merge_pull_request({workspace}/{repo_slug}#{pull_id})"
        )
        return response.json()

    # ── Issues ─────────────────────────────────────────────────────────────

    async def list_issues(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        state: str = "new",
        pagelen: int = 50,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/issues — state filter via ?q=."""
        params: Dict[str, Any] = {"pagelen": pagelen}
        if state:
            params["q"] = f'state="{state}"'
        response = await self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}/issues",
            access_token,
            params=params,
            context=f"list_issues({workspace}/{repo_slug})",
        )
        await self._raise_for_status(response, f"list_issues({workspace}/{repo_slug})")
        return response.json()

    async def get_issue(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        issue_id: int,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/issues/{id}."""
        response = await self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}/issues/{issue_id}",
            access_token,
            context=f"get_issue({workspace}/{repo_slug}#{issue_id})",
        )
        await self._raise_for_status(
            response, f"get_issue({workspace}/{repo_slug}#{issue_id})"
        )
        return response.json()

    async def create_issue(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /repositories/{ws}/{slug}/issues."""
        response = await self._request(
            "POST",
            f"/repositories/{workspace}/{repo_slug}/issues",
            access_token,
            json_body=body,
            context=f"create_issue({workspace}/{repo_slug})",
        )
        await self._raise_for_status(response, f"create_issue({workspace}/{repo_slug})")
        return response.json()

    # ── Commits ────────────────────────────────────────────────────────────

    async def list_commits(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        branch: Optional[str] = None,
        pagelen: int = 50,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/commits[/{branch}]."""
        path = f"/repositories/{workspace}/{repo_slug}/commits"
        if branch:
            path = f"{path}/{branch}"
        params = {"pagelen": pagelen}
        response = await self._request(
            "GET",
            path,
            access_token,
            params=params,
            context=f"list_commits({workspace}/{repo_slug})",
        )
        await self._raise_for_status(response, f"list_commits({workspace}/{repo_slug})")
        return response.json()

    async def get_commit(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        commit: str,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/commit/{node}."""
        response = await self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}/commit/{commit}",
            access_token,
            context=f"get_commit({workspace}/{repo_slug}@{commit})",
        )
        await self._raise_for_status(
            response, f"get_commit({workspace}/{repo_slug}@{commit})"
        )
        return response.json()

    # ── Snippets ───────────────────────────────────────────────────────────

    async def list_snippets(
        self,
        access_token: str,
        workspace: str,
        pagelen: int = 50,
    ) -> Dict[str, Any]:
        """GET /snippets/{workspace}."""
        params = {"pagelen": pagelen}
        response = await self._request(
            "GET",
            f"/snippets/{workspace}",
            access_token,
            params=params,
            context=f"list_snippets({workspace})",
        )
        await self._raise_for_status(response, f"list_snippets({workspace})")
        return response.json()

    # ── Webhooks ───────────────────────────────────────────────────────────

    async def list_webhooks(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/hooks."""
        response = await self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}/hooks",
            access_token,
            context=f"list_webhooks({workspace}/{repo_slug})",
        )
        await self._raise_for_status(
            response, f"list_webhooks({workspace}/{repo_slug})"
        )
        return response.json()

    async def create_webhook(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /repositories/{ws}/{slug}/hooks."""
        response = await self._request(
            "POST",
            f"/repositories/{workspace}/{repo_slug}/hooks",
            access_token,
            json_body=body,
            context=f"create_webhook({workspace}/{repo_slug})",
        )
        await self._raise_for_status(
            response, f"create_webhook({workspace}/{repo_slug})"
        )
        return response.json()

    # ── Files ──────────────────────────────────────────────────────────────

    async def get_file_content(
        self,
        access_token: str,
        workspace: str,
        repo_slug: str,
        commit: str,
        path: str,
    ) -> str:
        """GET /repositories/{ws}/{slug}/src/{commit}/{path} — returns raw text."""
        url = f"/repositories/{workspace}/{repo_slug}/src/{commit}/{path.lstrip('/')}"
        response = await self._request(
            "GET",
            url,
            access_token,
            context=f"get_file_content({workspace}/{repo_slug}@{commit}:{path})",
        )
        await self._raise_for_status(
            response, f"get_file_content({workspace}/{repo_slug}@{commit}:{path})"
        )
        return response.text
