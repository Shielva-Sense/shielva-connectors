"""All Mattermost API HTTP calls — zero business logic, zero normalization.

The base URL is tenant-specific: ``{server_url}/api/v4``. The client owns the
Bearer-auth header injection and the 4xx/5xx → connector-exception mapping.
Retry on 429/5xx with exponential backoff is built in (caller-opt-out via
``max_retries=0``). When the server returns ``Retry-After`` or
``X-RateLimit-Reset``, the backoff respects that hint.
"""
import asyncio
import random
from typing import Any, Dict, List, Optional, Tuple

import httpx
import structlog

from exceptions import (
    MattermostAuthError,
    MattermostBadRequestError,
    MattermostConflictError,
    MattermostError,
    MattermostNotFoundError,
    MattermostRateLimitError,
    MattermostServerError,
)

logger = structlog.get_logger(__name__)

_API_SUFFIX = "/api/v4"
_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_MAX_RETRIES = 3
_BASE_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 16.0


class MattermostHTTPClient:
    """Async HTTP client for the Mattermost REST API.

    Constructed with the tenant's ``server_url`` (e.g.
    ``https://mattermost.acme.com``) and an access token. The token is
    forwarded as ``Authorization: Bearer <token>`` on every call.
    """

    def __init__(
        self,
        server_url: str,
        access_token: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ):
        server_url = (server_url or "").rstrip("/")
        if server_url.endswith(_API_SUFFIX):
            server_url = server_url[: -len(_API_SUFFIX)]
        self._server_url = server_url
        self._base_url = f"{server_url}{_API_SUFFIX}" if server_url else _API_SUFFIX
        self._access_token = access_token or ""
        self._timeout_s = timeout_s
        self._max_retries = max_retries

    # ── URL & headers ───────────────────────────────────────────────────────

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _multipart_auth_headers(self) -> Dict[str, str]:
        """For ``/files`` multipart uploads — httpx sets Content-Type itself."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }

    # ── Error mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_message(body: Any) -> str:
        if isinstance(body, dict):
            return (
                body.get("message")
                or body.get("detailed_error")
                or body.get("error")
                or str(body)
            )
        return str(body) if body else ""

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}
        message = self._extract_message(body)
        suffix = f": {context}" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status == 400:
            raise MattermostBadRequestError(
                f"400 Bad Request{suffix}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status in (401, 403):
            raise MattermostAuthError(
                f"{status} Unauthorized{suffix}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise MattermostNotFoundError(
                f"404 Not Found{suffix}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise MattermostConflictError(
                f"409 Conflict{suffix}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = self._retry_after_seconds(response)
            raise MattermostRateLimitError(
                f"429 Rate limit{suffix}: {message}",
                status_code=429,
                response_body=body_dict,
                retry_after_s=retry_after,
            )
        if 500 <= status < 600:
            raise MattermostServerError(
                f"HTTP {status}{suffix}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise MattermostError(
            f"HTTP {status}{suffix}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        for hdr in ("Retry-After", "X-RateLimit-Reset"):
            v = response.headers.get(hdr)
            if not v:
                continue
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                continue
        return 5.0

    # ── Core request ────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        files: Optional[Any] = None,
        data: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        """Issue an authenticated request, retrying on 429 and 5xx."""
        url = self._build_url(path)
        headers = self._multipart_auth_headers() if (files or data) else self._auth_headers()
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body if not (files or data) else None,
                        files=files,
                        data=data,
                    )
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    try:
                        self._raise_for_status(response, context)
                    except (MattermostRateLimitError, MattermostServerError) as exc:
                        last_exc = exc
                        if attempt == self._max_retries:
                            raise
                        await self._sleep_backoff(attempt, response)
                        continue
                self._raise_for_status(response, context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = MattermostServerError(
                    f"Transport error{(': ' + context) if context else ''}: {exc}",
                )
                if attempt == self._max_retries:
                    raise last_exc
                await self._sleep_backoff(attempt, None)
                continue

        if last_exc:
            raise last_exc
        raise MattermostError(f"Unknown error{(': ' + context) if context else ''}")

    @staticmethod
    async def _sleep_backoff(attempt: int, response: Optional[httpx.Response]) -> None:
        delay = min(_BASE_BACKOFF_S * (2 ** attempt) + random.uniform(0, 0.25), _MAX_BACKOFF_S)
        if response is not None:
            retry_after = (
                response.headers.get("Retry-After")
                or response.headers.get("X-RateLimit-Reset")
            )
            try:
                if retry_after:
                    delay = max(delay, float(retry_after))
            except (TypeError, ValueError):
                pass
        await asyncio.sleep(delay)

    # ── System ──────────────────────────────────────────────────────────────

    async def ping(self) -> Dict[str, Any]:
        """GET /system/ping — liveness probe; no auth required by spec."""
        return await self._request("GET", "/system/ping", context="ping")

    # ── Users ───────────────────────────────────────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        return await self._request("GET", "/users/me", context="get_current_user")

    async def list_users(
        self,
        page: int = 0,
        per_page: int = 60,
        in_team_id: Optional[str] = None,
        in_channel_id: Optional[str] = None,
    ) -> Any:
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if in_team_id:
            params["in_team"] = in_team_id
        if in_channel_id:
            params["in_channel"] = in_channel_id
        return await self._request("GET", "/users", params=params, context="list_users")

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/users/{user_id}",
            context=f"get_user({user_id})",
        )

    async def create_user(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/users",
            json_body=payload,
            context="create_user",
        )

    async def search_users(
        self,
        team_id: str,
        term: str,
        in_channel_id: Optional[str] = None,
        not_in_channel_id: Optional[str] = None,
    ) -> Any:
        body: Dict[str, Any] = {"team_id": team_id, "term": term}
        if in_channel_id:
            body["in_channel_id"] = in_channel_id
        if not_in_channel_id:
            body["not_in_channel_id"] = not_in_channel_id
        return await self._request(
            "POST",
            "/users/search",
            json_body=body,
            context="search_users",
        )

    # ── Teams ───────────────────────────────────────────────────────────────

    async def list_teams(
        self,
        page: int = 0,
        per_page: int = 60,
        include_total_count: bool = False,
    ) -> Any:
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if include_total_count:
            params["include_total_count"] = "true"
        return await self._request(
            "GET",
            "/teams",
            params=params,
            context="list_teams",
        )

    async def get_team(self, team_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/teams/{team_id}",
            context=f"get_team({team_id})",
        )

    # ── Channels ────────────────────────────────────────────────────────────

    async def list_channels(
        self,
        team_id: str,
        page: int = 0,
        per_page: int = 60,
        include_deleted: bool = False,
    ) -> Any:
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if include_deleted:
            params["include_deleted"] = "true"
        return await self._request(
            "GET",
            f"/teams/{team_id}/channels",
            params=params,
            context=f"list_channels({team_id})",
        )

    async def get_channel(self, channel_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/channels/{channel_id}",
            context=f"get_channel({channel_id})",
        )

    async def create_channel(
        self,
        team_id: str,
        name: str,
        display_name: str,
        type: str = "O",
        purpose: str = "",
        header: str = "",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "team_id": team_id,
            "name": name,
            "display_name": display_name,
            "type": type,
            "purpose": purpose,
            "header": header,
        }
        return await self._request(
            "POST",
            "/channels",
            json_body=body,
            context="create_channel",
        )

    async def delete_channel(self, channel_id: str) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/channels/{channel_id}",
            context=f"delete_channel({channel_id})",
        )

    async def add_user_to_channel(self, channel_id: str, user_id: str) -> Dict[str, Any]:
        body = {"user_id": user_id}
        return await self._request(
            "POST",
            f"/channels/{channel_id}/members",
            json_body=body,
            context=f"add_user_to_channel({channel_id})",
        )

    # ── Posts ───────────────────────────────────────────────────────────────

    async def post_message(
        self,
        channel_id: str,
        message: str,
        root_id: Optional[str] = None,
        props: Optional[Dict[str, Any]] = None,
        file_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        if props is not None:
            body["props"] = props
        if file_ids:
            body["file_ids"] = file_ids
        return await self._request(
            "POST",
            "/posts",
            json_body=body,
            context="post_message",
        )

    async def get_post(self, post_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/posts/{post_id}",
            context=f"get_post({post_id})",
        )

    async def update_post(
        self,
        post_id: str,
        message: Optional[str] = None,
        props: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"id": post_id}
        if message is not None:
            body["message"] = message
        if props is not None:
            body["props"] = props
        return await self._request(
            "PUT",
            f"/posts/{post_id}",
            json_body=body,
            context=f"update_post({post_id})",
        )

    async def delete_post(self, post_id: str) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/posts/{post_id}",
            context=f"delete_post({post_id})",
        )

    async def list_channel_posts(
        self,
        channel_id: str,
        page: int = 0,
        per_page: int = 60,
        since: Optional[int] = None,
        before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if since is not None:
            params["since"] = since
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        return await self._request(
            "GET",
            f"/channels/{channel_id}/posts",
            params=params,
            context=f"list_channel_posts({channel_id})",
        )

    # ── Files ───────────────────────────────────────────────────────────────

    async def upload_file(
        self,
        channel_id: str,
        file_bytes: bytes,
        filename: str,
    ) -> Dict[str, Any]:
        """POST /files — multipart upload for a single file."""
        data = {"channel_id": channel_id}
        files: Dict[str, Tuple[str, bytes, str]] = {
            "files": (filename, file_bytes, "application/octet-stream"),
        }
        return await self._request(
            "POST",
            "/files",
            data=data,
            files=files,
            context=f"upload_file({filename})",
        )

    async def get_file_info(self, file_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/files/{file_id}/info",
            context=f"get_file_info({file_id})",
        )

    # ── Incoming + outgoing webhooks ────────────────────────────────────────

    async def create_incoming_webhook(
        self,
        channel_id: str,
        display_name: str,
        description: str = "",
        username: Optional[str] = None,
        icon_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "channel_id": channel_id,
            "display_name": display_name,
            "description": description,
        }
        if username is not None:
            body["username"] = username
        if icon_url is not None:
            body["icon_url"] = icon_url
        return await self._request(
            "POST",
            "/hooks/incoming",
            json_body=body,
            context="create_incoming_webhook",
        )

    async def list_incoming_webhooks(
        self,
        team_id: Optional[str] = None,
        page: int = 0,
        per_page: int = 60,
    ) -> Any:
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if team_id:
            params["team_id"] = team_id
        return await self._request(
            "GET",
            "/hooks/incoming",
            params=params,
            context="list_incoming_webhooks",
        )

    async def create_outgoing_webhook(
        self,
        team_id: str,
        display_name: str,
        trigger_words: List[str],
        callback_urls: List[str],
        channel_id: Optional[str] = None,
        description: str = "",
        content_type: str = "application/x-www-form-urlencoded",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "team_id": team_id,
            "display_name": display_name,
            "trigger_words": trigger_words,
            "callback_urls": callback_urls,
            "description": description,
            "content_type": content_type,
        }
        if channel_id:
            body["channel_id"] = channel_id
        return await self._request(
            "POST",
            "/hooks/outgoing",
            json_body=body,
            context="create_outgoing_webhook",
        )

    async def list_outgoing_webhooks(
        self,
        team_id: Optional[str] = None,
        page: int = 0,
        per_page: int = 60,
    ) -> Any:
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if team_id:
            params["team_id"] = team_id
        return await self._request(
            "GET",
            "/hooks/outgoing",
            params=params,
            context="list_outgoing_webhooks",
        )

    # ── Bots ────────────────────────────────────────────────────────────────

    async def list_bots(
        self,
        page: int = 0,
        per_page: int = 60,
        include_deleted: bool = False,
    ) -> Any:
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if include_deleted:
            params["include_deleted"] = "true"
        return await self._request(
            "GET",
            "/bots",
            params=params,
            context="list_bots",
        )

    # ── Commands ────────────────────────────────────────────────────────────

    async def list_team_commands(
        self,
        team_id: str,
        custom_only: bool = False,
    ) -> Any:
        params: Dict[str, Any] = {"team_id": team_id}
        if custom_only:
            params["custom_only"] = "true"
        return await self._request(
            "GET",
            "/commands",
            params=params,
            context=f"list_team_commands({team_id})",
        )
