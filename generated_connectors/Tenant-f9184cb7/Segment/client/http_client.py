from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    SegmentAuthError,
    SegmentError,
    SegmentNetworkError,
    SegmentNotFoundError,
    SegmentRateLimitError,
    SegmentServerError,
)

SEGMENT_BASE_URL = "https://api.segmentapis.com"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_PAGE_SIZE = 200


class SegmentHTTPClient:
    """Low-level async HTTP client for the Segment Public API v1.

    Uses Bearer token authentication.
    Segment uses cursor-based pagination:
        { "data": { "<resource>": [...] }, "pagination": { "current": "...", "next": "..." } }
    The ``next`` cursor is an opaque string — pass it as the ``pagination[cursor]`` query param.
    """

    def __init__(self, access_token: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._access_token = access_token
        self._client = httpx.AsyncClient(
            base_url=SEGMENT_BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise SegmentNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise SegmentNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 202, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        # Segment Public API errors: { "errors": [{ "message": "...", "type": "..." }] }
        errors = body.get("errors", [])
        if errors:
            first = errors[0]
            err_msg = first.get("message", response.text or "Unknown Segment error")
            err_code = first.get("type", "")
        else:
            err_msg = body.get("message", response.text or "Unknown Segment error")
            err_code = body.get("type", "")

        if response.status_code == 401:
            raise SegmentAuthError(f"Authentication failed: {err_msg}", 401, err_code)
        if response.status_code == 403:
            raise SegmentAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status_code == 404:
            raise SegmentNotFoundError("resource", path)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise SegmentRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise SegmentServerError(
                f"Segment server error {response.status_code}: {err_msg}",
                response.status_code,
            )

        raise SegmentError(
            f"Segment error {response.status_code}: {err_msg}",
            response.status_code,
            err_code,
        )

    # ── Workspaces ───────────────────────────────────────────────────────────

    async def get_workspace(self) -> dict[str, Any]:
        """GET /workspaces — returns the single workspace for the token."""
        return await self._request("GET", "/workspaces")

    # ── Sources ───────────────────────────────────────────────────────────────

    async def list_sources(
        self,
        pagination_cursor: str | None = None,
        count: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """GET /sources — cursor-based pagination."""
        params: dict[str, Any] = {"pagination[count]": count}
        if pagination_cursor:
            params["pagination[cursor]"] = pagination_cursor
        return await self._request("GET", "/sources", params=params)

    async def get_source(self, source_id: str) -> dict[str, Any]:
        """GET /sources/{source_id}."""
        return await self._request("GET", f"/sources/{source_id}")

    # ── Destinations ──────────────────────────────────────────────────────────

    async def list_destinations(self, source_id: str) -> dict[str, Any]:
        """GET /sources/{source_id}/destinations — all destinations for a source."""
        return await self._request("GET", f"/sources/{source_id}/destinations")

    # ── Spaces ────────────────────────────────────────────────────────────────

    async def list_spaces(self) -> dict[str, Any]:
        """GET /spaces — all Segment Profiles AI spaces."""
        return await self._request("GET", "/spaces")

    # ── Functions ─────────────────────────────────────────────────────────────

    async def list_functions(
        self,
        pagination_cursor: str | None = None,
        count: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """GET /functions — cursor-based pagination."""
        params: dict[str, Any] = {"pagination[count]": count}
        if pagination_cursor:
            params["pagination[cursor]"] = pagination_cursor
        return await self._request("GET", "/functions", params=params)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SegmentHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
