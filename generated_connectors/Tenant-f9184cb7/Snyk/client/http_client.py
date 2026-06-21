"""All Snyk API HTTP calls — zero business logic, zero normalization.

Two surfaces are supported:

- REST v3 at ``https://api.snyk.io/rest`` — JSON:API, requires the
  ``?version=YYYY-MM-DD`` query parameter, content type
  ``application/vnd.api+json``.
- Legacy v1 at ``https://api.snyk.io/v1`` — plain JSON.

Authentication is identical on both surfaces: ``Authorization: token <api_token>``
(literal prefix ``token`` — **NOT** ``Bearer``).

Retry on 429 + 5xx with exponential backoff. ``Retry-After`` is honoured when
the server provides it. All errors map to typed exceptions from
``exceptions.py``.
"""
import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    SnykAuthError,
    SnykBadRequestError,
    SnykConflictError,
    SnykError,
    SnykNotFoundError,
    SnykRateLimitError,
    SnykServerError,
)

logger = structlog.get_logger(__name__)

_REST_BASE = "https://api.snyk.io/rest"
_V1_BASE = "https://api.snyk.io/v1"
_DEFAULT_VERSION = "2024-10-15"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class SnykHTTPClient:
    """Thin async httpx client for the Snyk REST v3 + legacy v1 APIs.

    The connector holds a single instance and passes the API token on every
    call so token rotation is centralised in `SnykConnector`.
    """

    def __init__(
        self,
        api_token: str = "",
        rest_base_url: str = _REST_BASE,
        v1_base_url: str = _V1_BASE,
        default_version: str = _DEFAULT_VERSION,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._api_token = api_token or ""
        self._rest_base = (rest_base_url or _REST_BASE).rstrip("/")
        self._v1_base = (v1_base_url or _V1_BASE).rstrip("/")
        self._default_version = default_version or _DEFAULT_VERSION
        self._timeout = timeout

    # ── Header builders ────────────────────────────────────────────────────

    def _rest_headers(self) -> Dict[str, str]:
        # Snyk requires the literal "token" prefix — NOT "Bearer".
        return {
            "Authorization": f"token {self._api_token}",
            "Content-Type": "application/vnd.api+json",
            "Accept": "application/vnd.api+json",
        }

    def _v1_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"token {self._api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Error mapping ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_message(body: Any) -> str:
        if not isinstance(body, dict):
            return ""
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return (
                    first.get("detail")
                    or first.get("title")
                    or first.get("message", "")
                )
        if "message" in body:
            return str(body["message"])
        err = body.get("error")
        if isinstance(err, dict):
            return err.get("message", "") or str(err)
        if isinstance(err, str):
            return err
        return ""

    def _raise_for_status(
        self, response: httpx.Response, context: str = ""
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}
        if not isinstance(body, dict):
            body = {"raw": body}

        message = self._extract_message(body) or response.text or f"HTTP {status}"
        suffix = f": {context}" if context else ""

        if status == 400:
            raise SnykBadRequestError(
                f"400 Bad Request{suffix}: {message}",
                status_code=400,
                response_body=body,
            )
        if status in (401, 403):
            raise SnykAuthError(
                f"{status} Unauthorized{suffix}: {message}",
                status_code=status,
                response_body=body,
            )
        if status == 404:
            raise SnykNotFoundError(
                f"404 Not Found{suffix}: {message}",
                status_code=404,
                response_body=body,
            )
        if status == 409:
            raise SnykConflictError(
                f"409 Conflict{suffix}: {message}",
                status_code=409,
                response_body=body,
            )
        if status == 429:
            retry_after = self._retry_after_seconds(response, attempt=0)
            raise SnykRateLimitError(
                f"429 Rate limit{suffix}: {message}",
                status_code=429,
                response_body=body,
                retry_after_s=retry_after,
            )
        if status >= 500:
            raise SnykServerError(
                f"HTTP {status}{suffix}: {message}",
                status_code=status,
                response_body=body,
            )
        raise SnykError(
            f"HTTP {status}{suffix}: {message}",
            status_code=status,
            response_body=body,
        )

    # ── Core request wrapper ───────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        """Issue a request with retry on 429 + 5xx + transport errors.

        Returns whatever ``response.json()`` produces (dict for REST v3 and
        most v1 surfaces, list for v1 array-returning endpoints like
        ``/org/{id}/members``), ``{}`` on 204/empty, or ``{"raw": text}`` if
        the body is not JSON.
        """
        attempt = 0
        last_exc: Optional[Exception] = None
        while True:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt >= _MAX_RETRIES - 1:
                    raise SnykServerError(
                        f"network error{': ' + context if context else ''}: {exc}",
                    ) from exc
                delay = _BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "snyk.http.transport_retry",
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < _MAX_RETRIES - 1:
                    delay = self._retry_after_seconds(response, attempt)
                    logger.warning(
                        "snyk.http.retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue

            self._raise_for_status(response, context)
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except Exception:
                return {"raw": response.text}

    @staticmethod
    def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return _BACKOFF_BASE * (2 ** attempt)

    # ── Param helpers ──────────────────────────────────────────────────────

    def _rest_params(
        self,
        version: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"version": version or self._default_version}
        if extra:
            for key, value in extra.items():
                if value is None:
                    continue
                if isinstance(value, list):
                    if not value:
                        continue
                    params[key] = ",".join(str(v) for v in value)
                else:
                    params[key] = value
        return params

    # ── Health / identity ──────────────────────────────────────────────────

    async def get_self(self) -> Dict[str, Any]:
        """GET /v1/user/me — current Snyk user; used as the health probe."""
        url = f"{self._v1_base}/user/me"
        return await self._request(
            "GET",
            url,
            headers=self._v1_headers(),
            context="get_self",
        )

    # ── REST v3 endpoints ──────────────────────────────────────────────────

    async def list_organizations(
        self,
        version: Optional[str] = None,
        limit: int = 100,
        starting_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._rest_base}/orgs"
        extra = {"limit": limit, "starting_after": starting_after}
        return await self._request(
            "GET",
            url,
            headers=self._rest_headers(),
            params=self._rest_params(version, extra),
            context="list_organizations",
        )

    async def get_organization(
        self, org_id: str, version: Optional[str] = None
    ) -> Dict[str, Any]:
        url = f"{self._rest_base}/orgs/{org_id}"
        return await self._request(
            "GET",
            url,
            headers=self._rest_headers(),
            params=self._rest_params(version),
            context=f"get_organization({org_id})",
        )

    async def list_projects(
        self,
        org_id: str,
        target_id: Optional[str] = None,
        types: Optional[List[str]] = None,
        version: Optional[str] = None,
        limit: int = 100,
        starting_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._rest_base}/orgs/{org_id}/projects"
        extra: Dict[str, Any] = {
            "limit": limit,
            "starting_after": starting_after,
            "target_id": target_id,
            "types": types,
        }
        return await self._request(
            "GET",
            url,
            headers=self._rest_headers(),
            params=self._rest_params(version, extra),
            context=f"list_projects({org_id})",
        )

    async def get_project(
        self,
        org_id: str,
        project_id: str,
        version: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._rest_base}/orgs/{org_id}/projects/{project_id}"
        return await self._request(
            "GET",
            url,
            headers=self._rest_headers(),
            params=self._rest_params(version),
            context=f"get_project({project_id})",
        )

    async def delete_project(
        self,
        org_id: str,
        project_id: str,
        version: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._rest_base}/orgs/{org_id}/projects/{project_id}"
        return await self._request(
            "DELETE",
            url,
            headers=self._rest_headers(),
            params=self._rest_params(version),
            context=f"delete_project({project_id})",
        )

    async def list_issues(
        self,
        org_id: str,
        project_id: Optional[str] = None,
        severity: Optional[List[str]] = None,
        type: Optional[str] = None,
        limit: int = 50,
        starting_after: Optional[str] = None,
        version: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._rest_base}/orgs/{org_id}/issues"
        extra: Dict[str, Any] = {
            "limit": limit,
            "starting_after": starting_after,
            "project_id": project_id,
            "severity": severity,
            "type": type,
        }
        return await self._request(
            "GET",
            url,
            headers=self._rest_headers(),
            params=self._rest_params(version, extra),
            context=f"list_issues({org_id})",
        )

    async def get_issue(
        self,
        org_id: str,
        issue_id: str,
        version: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._rest_base}/orgs/{org_id}/issues/{issue_id}"
        return await self._request(
            "GET",
            url,
            headers=self._rest_headers(),
            params=self._rest_params(version),
            context=f"get_issue({issue_id})",
        )

    async def list_targets(
        self,
        org_id: str,
        source: Optional[str] = None,
        limit: int = 100,
        starting_after: Optional[str] = None,
        version: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._rest_base}/orgs/{org_id}/targets"
        extra: Dict[str, Any] = {
            "limit": limit,
            "starting_after": starting_after,
            "source": source,
        }
        return await self._request(
            "GET",
            url,
            headers=self._rest_headers(),
            params=self._rest_params(version, extra),
            context=f"list_targets({org_id})",
        )

    async def get_target(
        self,
        org_id: str,
        target_id: str,
        version: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._rest_base}/orgs/{org_id}/targets/{target_id}"
        return await self._request(
            "GET",
            url,
            headers=self._rest_headers(),
            params=self._rest_params(version),
            context=f"get_target({target_id})",
        )

    # ── Legacy v1 endpoints ────────────────────────────────────────────────

    async def list_dependencies(
        self,
        org_id: str,
        project_id: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """POST /v1/org/{id}/dependencies — paginated POST search."""
        url = f"{self._v1_base}/org/{org_id}/dependencies"
        body: Dict[str, Any] = {"filters": {}}
        if project_id:
            body["filters"]["projects"] = [project_id]
        params = {"perPage": limit, "page": 1}
        return await self._request(
            "POST",
            url,
            headers=self._v1_headers(),
            params=params,
            json_body=body,
            context=f"list_dependencies({org_id})",
        )

    async def list_org_members(self, org_id: str) -> Dict[str, Any]:
        """GET /v1/org/{id}/members — list of org members (legacy v1)."""
        url = f"{self._v1_base}/org/{org_id}/members"
        return await self._request(
            "GET",
            url,
            headers=self._v1_headers(),
            context=f"list_org_members({org_id})",
        )

    async def get_user_settings(self, org_id: str) -> Dict[str, Any]:
        """GET /v1/user/me/notification-settings/org/{id} — per-org user prefs."""
        url = f"{self._v1_base}/user/me/notification-settings/org/{org_id}"
        return await self._request(
            "GET",
            url,
            headers=self._v1_headers(),
            context=f"get_user_settings({org_id})",
        )
