from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    BugsnagAuthError,
    BugsnagError,
    BugsnagNetworkError,
    BugsnagNotFoundError,
    BugsnagRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_BASE_URL: str = "https://api.bugsnag.com"


class BugsnagHTTPClient:
    """Low-level async HTTP client for the Bugsnag Data Access API v2.

    Auth: ``Authorization: token {auth_token}`` (note: ``token`` prefix, NOT ``Bearer``).
    Base: ``https://api.bugsnag.com``.
    Pagination: ``X-Next-Page-Link`` response header (absolute URL to next page),
                or ``total-page-count`` + per_page_offset for offset-based pagination.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._auth_token: str = cfg.get("auth_token", "")
        base = (cfg.get("base_url", "") or DEFAULT_BASE_URL).rstrip("/")
        self._base_url: str = base
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self._auth_token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """Perform an HTTP request and return (parsed_body, response_headers).

        ``url`` may be a full absolute URL or a path relative to the base URL.
        """
        if not url.startswith("http"):
            url = f"{self._base_url}{url}"
        headers = self._make_headers()
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    body, resp_headers = await self._handle_response(response)
                    return body, resp_headers
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise BugsnagNetworkError(f"Network error: {exc}") from exc
        except (
            BugsnagError,
            BugsnagAuthError,
            BugsnagRateLimitError,
            BugsnagNotFoundError,
            BugsnagNetworkError,
        ):
            raise
        except Exception as exc:
            raise BugsnagNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> tuple[Any, dict[str, str]]:
        status = response.status
        resp_headers = dict(response.headers)

        if status in (200, 201):
            try:
                body = await response.json()
            except Exception:
                body = await response.text()
            return body, resp_headers

        body: Any = {}
        try:
            body = await response.json()
        except Exception:
            pass

        self._raise_for_status(status, body)
        raise BugsnagError(f"HTTP {status}", status_code=status)

    def _raise_for_status(self, status: int, body: Any) -> None:
        """Map HTTP status codes to typed Bugsnag exceptions."""
        if isinstance(body, dict):
            err_msg: str = (
                body.get("errors", [""])[0]
                if isinstance(body.get("errors"), list)
                else body.get("message", "") or f"HTTP {status}"
            )
        else:
            err_msg = str(body) if body else f"HTTP {status}"

        if status in (401, 403):
            raise BugsnagAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise BugsnagNotFoundError("resource", err_msg)
        if status == 429:
            raise BugsnagRateLimitError(
                f"Rate limited: {err_msg}", retry_after=0.0
            )
        if status >= 500:
            raise BugsnagNetworkError(
                f"Bugsnag server error {status}: {err_msg}",
                status_code=status,
            )
        raise BugsnagError(
            f"Bugsnag error {status}: {err_msg}", status_code=status
        )

    # ── User / Organizations ───────────────────────────────────────────────────

    async def get_organizations(self) -> list[dict[str, Any]]:
        """GET /user/organizations — list all organizations the user belongs to."""
        body, _ = await self._request("GET", "/user/organizations")
        return body if isinstance(body, list) else []  # type: ignore[return-value]

    async def get_organization(self, org_slug: str) -> dict[str, Any]:
        """GET /organizations/{org_slug} — fetch a single organization."""
        body, _ = await self._request("GET", f"/organizations/{org_slug}")
        return body  # type: ignore[return-value]

    # ── Projects ──────────────────────────────────────────────────────────────

    async def get_projects(
        self,
        org_slug: str,
        per_page: int = 100,
        offset: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /organizations/{org_slug}/projects — per_page + offset pagination.

        Returns (items, next_page_url | None).
        """
        params: dict[str, Any] = {"per_page": per_page}
        if offset is not None:
            params["offset"] = offset

        body, headers = await self._request(
            "GET", f"/organizations/{org_slug}/projects", params=params
        )
        items: list[dict[str, Any]] = body if isinstance(body, list) else []
        next_url: str | None = headers.get("X-Next-Page-Link") or headers.get("x-next-page-link")
        return items, next_url

    # ── Errors ────────────────────────────────────────────────────────────────

    async def get_errors(
        self,
        project_id: str,
        per_page: int = 25,
        per_page_offset: int | None = None,
        severity: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /projects/{project_id}/errors — list errors with optional severity filter.

        Returns (items, next_page_url | None).
        Pagination: ``X-Next-Page-Link`` header.
        """
        params: dict[str, Any] = {"per_page": per_page}
        if per_page_offset is not None:
            params["per_page_offset"] = per_page_offset
        if severity:
            params["filters[severity]"] = severity

        body, headers = await self._request(
            "GET", f"/projects/{project_id}/errors", params=params
        )
        items: list[dict[str, Any]] = body if isinstance(body, list) else []
        next_url: str | None = headers.get("X-Next-Page-Link") or headers.get("x-next-page-link")
        return items, next_url

    async def get_error(self, project_id: str, error_id: str) -> dict[str, Any]:
        """GET /projects/{project_id}/errors/{error_id} — fetch a single error."""
        body, _ = await self._request(
            "GET", f"/projects/{project_id}/errors/{error_id}"
        )
        return body  # type: ignore[return-value]

    # ── Releases ──────────────────────────────────────────────────────────────

    async def get_releases(
        self,
        project_id: str,
        per_page: int = 25,
    ) -> list[dict[str, Any]]:
        """GET /projects/{project_id}/releases — list releases for a project."""
        params: dict[str, Any] = {"per_page": per_page}
        body, _ = await self._request(
            "GET", f"/projects/{project_id}/releases", params=params
        )
        return body if isinstance(body, list) else []  # type: ignore[return-value]

    # ── Collaborators ─────────────────────────────────────────────────────────

    async def get_collaborators(self, org_slug: str) -> list[dict[str, Any]]:
        """GET /organizations/{org_slug}/collaborators — list org collaborators."""
        body, _ = await self._request(
            "GET", f"/organizations/{org_slug}/collaborators"
        )
        return body if isinstance(body, list) else []  # type: ignore[return-value]
