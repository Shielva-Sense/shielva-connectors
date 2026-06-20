from __future__ import annotations

import re
from typing import Any

import aiohttp

from exceptions import (
    SentryAuthError,
    SentryError,
    SentryNetworkError,
    SentryNotFoundError,
    SentryRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_BASE_URL: str = "https://sentry.io"

# Matches the "next" cursor URL in Sentry Link headers:
# Link: <url>; rel="next"; results="true"; cursor="..."
_LINK_NEXT_RE = re.compile(
    r'<([^>]+)>;\s*rel="next";\s*results="true"'
)


def _parse_next_cursor(link_header: str) -> str | None:
    """Extract the next-page URL from a Sentry Link header, or None if no more pages."""
    if not link_header:
        return None
    match = _LINK_NEXT_RE.search(link_header)
    if match:
        return match.group(1)
    return None


class SentryHTTPClient:
    """Low-level async HTTP client for the Sentry REST API v0.

    Auth: ``Authorization: Bearer {auth_token}``
    Base: ``{base_url}/api/0/``
    Pagination: Link header cursor (rel="next"; results="true").
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._auth_token: str = cfg.get("auth_token", "")
        base = (cfg.get("base_url", "") or DEFAULT_BASE_URL).rstrip("/")
        self._api_base: str = f"{base}/api/0"
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._auth_token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """Perform an HTTP request and return (parsed_body, response_headers)."""
        url = f"{self._api_base}{path}"
        headers = self._make_headers()
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    body, resp_headers = await self._handle_response(response)
                    return body, resp_headers
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise SentryNetworkError(f"Network error: {exc}") from exc
        except (
            SentryError,
            SentryAuthError,
            SentryRateLimitError,
            SentryNotFoundError,
            SentryNetworkError,
        ):
            raise
        except Exception as exc:
            raise SentryNetworkError(f"Unexpected network error: {exc}") from exc

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
        raise SentryError(f"HTTP {status}", status_code=status)

    def _raise_for_status(self, status: int, body: Any) -> None:
        """Map HTTP status codes to typed Sentry exceptions."""
        if isinstance(body, dict):
            err_msg: str = (
                body.get("detail", "")
                or body.get("error", "")
                or f"HTTP {status}"
            )
        else:
            err_msg = str(body) if body else f"HTTP {status}"

        if status in (401, 403):
            raise SentryAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise SentryNotFoundError("resource", err_msg)
        if status == 429:
            raise SentryRateLimitError(
                f"Rate limited: {err_msg}", retry_after=0.0
            )
        if status >= 500:
            raise SentryNetworkError(
                f"Sentry server error {status}: {err_msg}",
                status_code=status,
            )
        raise SentryError(
            f"Sentry error {status}: {err_msg}", status_code=status
        )

    # ── Organization ──────────────────────────────────────────────────────────

    async def get_organization(self, org_slug: str) -> dict[str, Any]:
        """GET /organizations/{org_slug}/ — verify credentials + org access."""
        body, _ = await self._request("GET", f"/organizations/{org_slug}/")
        return body  # type: ignore[return-value]

    # ── Projects ──────────────────────────────────────────────────────────────

    async def get_projects(self, org_slug: str) -> list[dict[str, Any]]:
        """GET /organizations/{org_slug}/projects/ — fetch all projects.

        Follows Link header cursor pagination exhaustively.
        """
        results: list[dict[str, Any]] = []
        url = f"/organizations/{org_slug}/projects/"
        params: dict[str, Any] = {"limit": 100}

        while url:
            body, headers = await self._request("GET", url, params=params)
            page: list[dict[str, Any]] = body if isinstance(body, list) else []
            results.extend(page)
            link_header = headers.get("Link", "") or headers.get("link", "")
            next_url = _parse_next_cursor(link_header)
            if next_url and page:
                # next_url is an absolute URL — strip the base and use path only
                url = next_url.replace(self._api_base, "")
                params = {}
            else:
                break

        return results

    # ── Issues ────────────────────────────────────────────────────────────────

    async def get_issues(
        self,
        org_slug: str,
        project: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /organizations/{org_slug}/issues/ — cursor-paginated issue list.

        Returns (items, next_cursor_url | None).
        """
        path = f"/organizations/{org_slug}/issues/"
        params: dict[str, Any] = {"limit": limit}
        if project:
            params["project"] = project
        if cursor:
            params["cursor"] = cursor

        body, headers = await self._request("GET", path, params=params)
        items: list[dict[str, Any]] = body if isinstance(body, list) else []
        link_header = headers.get("Link", "") or headers.get("link", "")
        next_url = _parse_next_cursor(link_header)
        return items, next_url

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        """GET /issues/{issue_id}/ — fetch a single issue by ID."""
        body, _ = await self._request("GET", f"/issues/{issue_id}/")
        return body  # type: ignore[return-value]

    async def get_issue_events(
        self, issue_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """GET /issues/{issue_id}/events/ — fetch events for an issue."""
        path = f"/issues/{issue_id}/events/"
        body, _ = await self._request("GET", path, params={"limit": limit, "full": "true"})
        return body if isinstance(body, list) else []  # type: ignore[return-value]

    # ── Releases ──────────────────────────────────────────────────────────────

    async def get_releases(
        self,
        org_slug: str,
        project: str | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /organizations/{org_slug}/releases/ — cursor-paginated release list.

        Returns (items, next_cursor_url | None).
        """
        path = f"/organizations/{org_slug}/releases/"
        params: dict[str, Any] = {"limit": 100}
        if project:
            params["project"] = project
        if cursor:
            params["cursor"] = cursor

        body, headers = await self._request("GET", path, params=params)
        items: list[dict[str, Any]] = body if isinstance(body, list) else []
        link_header = headers.get("Link", "") or headers.get("link", "")
        next_url = _parse_next_cursor(link_header)
        return items, next_url
