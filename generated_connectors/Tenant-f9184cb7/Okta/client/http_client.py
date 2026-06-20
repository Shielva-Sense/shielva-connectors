from __future__ import annotations

import re
from typing import Any

import httpx

from exceptions import (
    OktaAuthError,
    OktaError,
    OktaNetworkError,
    OktaNotFoundError,
    OktaRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0


class OktaHTTPClient:
    """Low-level async HTTP client for the Okta REST API v1.

    Uses SSWS token authentication:
        Authorization: SSWS {api_token}
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._api_token: str = cfg.get("api_token", "")
        self._domain: str = cfg.get("domain", "").rstrip("/")
        base_url = f"https://{self._domain}/api/v1" if self._domain else ""
        self._base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"SSWS {self._api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _parse_next_cursor(self, response: httpx.Response) -> str | None:
        """Parse Link header and return the 'after' cursor from the next page URL."""
        link_header = response.headers.get("Link", "")
        if not link_header:
            return None
        # Look for rel="next" URL
        match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
        if not match:
            return None
        next_url = match.group(1)
        # Extract 'after' query param from the next URL
        after_match = re.search(r"[?&]after=([^&]+)", next_url)
        return after_match.group(1) if after_match else None

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map Okta HTTP error codes to typed exceptions."""
        err_msg: str = (
            body.get("errorSummary")
            or body.get("error_description")
            or body.get("message")
            or f"HTTP {status}"
        )
        if status in (401, 403):
            raise OktaAuthError(
                f"Okta authentication failed ({status}): {err_msg}",
                status_code=status,
                code="unauthorized" if status == 401 else "forbidden",
            )
        if status == 404:
            raise OktaNotFoundError("resource", err_msg)
        if status == 429:
            raise OktaRateLimitError(f"Okta rate limit exceeded: {err_msg}")
        if status >= 500:
            raise OktaNetworkError(
                f"Okta server error {status}: {err_msg}",
                status_code=status,
            )
        raise OktaError(f"Okta error {status}: {err_msg}", status_code=status)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute an HTTP request and return parsed JSON."""
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise OktaNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise OktaNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        self._raise_for_status(response.status_code, body)

    async def _paginate(
        self,
        path: str,
        limit: int,
        after: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> tuple[list[Any], str | None]:
        """Fetch a single page from an Okta list endpoint.

        Returns (items, next_cursor). Caller is responsible for iterating pages.
        """
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        if extra_params:
            params.update(extra_params)

        try:
            response = await self._client.request("GET", path, params=params)
        except httpx.TimeoutException as exc:
            raise OktaNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise OktaNetworkError(f"Network error: {exc}") from exc

        if response.status_code not in (200, 201):
            body: dict[str, Any] = {}
            try:
                body = response.json()
            except Exception:
                pass
            self._raise_for_status(response.status_code, body)

        items: list[Any] = response.json() if response.content else []
        next_cursor = self._parse_next_cursor(response)
        return items, next_cursor

    # ── Auth probe ────────────────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """GET /users/me — validate token and return current user info."""
        result = await self._request("GET", "/users/me")
        return result  # type: ignore[return-value]

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(
        self,
        limit: int = 200,
        after: str | None = None,
        **params: Any,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /users — list users with cursor pagination.

        Returns (users, next_cursor).
        """
        return await self._paginate("/users", limit=limit, after=after, extra_params=params or None)

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """GET /users/{user_id} — fetch a single user."""
        result = await self._request("GET", f"/users/{user_id}")
        return result  # type: ignore[return-value]

    # ── Groups ────────────────────────────────────────────────────────────────

    async def get_groups(
        self,
        limit: int = 200,
        after: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /groups — list groups with cursor pagination."""
        return await self._paginate("/groups", limit=limit, after=after)

    # ── Apps ──────────────────────────────────────────────────────────────────

    async def get_apps(
        self,
        limit: int = 200,
        after: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /apps — list applications with cursor pagination."""
        return await self._paginate("/apps", limit=limit, after=after)

    # ── System Logs ───────────────────────────────────────────────────────────

    async def get_logs(
        self,
        limit: int = 100,
        after: str | None = None,
        since: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /logs — list system log events with cursor pagination.

        Args:
            since: ISO 8601 datetime string for filtering events after a timestamp.
        """
        extra: dict[str, Any] = {}
        if since:
            extra["since"] = since
        return await self._paginate("/logs", limit=limit, after=after, extra_params=extra or None)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OktaHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
