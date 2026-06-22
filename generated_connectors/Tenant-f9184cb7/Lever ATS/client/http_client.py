from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    LeverAuthError,
    LeverError,
    LeverNetworkError,
    LeverNotFoundError,
    LeverRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
LEVER_API_BASE: str = "https://api.lever.co/v1"


class LeverHTTPClient:
    """Low-level async HTTP client for the Lever Data API v1.

    Uses HTTP Basic Auth with the API key as the username and an empty
    password, per Lever's documented auth scheme:
        Authorization: Basic base64(api_key:)
    All requests include Accept: application/json.
    Lever responses have the shape: {"data": [...], "hasNext": bool, "next": "cursor"}
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._config: dict[str, Any] = config or {}
        self._api_key: str = self._config.get("api_key", "")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _auth(self, api_key: str = "") -> aiohttp.BasicAuth:
        key = api_key or self._api_key
        return aiohttp.BasicAuth(key, "")

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        api_key: str = "",
    ) -> dict[str, Any]:
        auth = self._auth(api_key)
        headers = {
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    auth=auth,
                    params=params,
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise LeverNetworkError(f"Network error: {exc}") from exc
        except (
            LeverError,
            LeverAuthError,
            LeverRateLimitError,
            LeverNotFoundError,
            LeverNetworkError,
        ):
            raise
        except Exception as exc:
            raise LeverNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        # Attempt to read error body
        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        self._raise_for_status(status, body)
        # Should never reach here — _raise_for_status always raises
        raise LeverError(f"HTTP {status}")  # pragma: no cover

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map Lever HTTP error codes to typed exceptions."""
        err_msg: str = (
            body.get("error", "")
            or body.get("message", "")
            or body.get("description", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise LeverAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise LeverNotFoundError("resource", err_msg)
        if status == 429:
            retry_after_raw = body.get("retryAfter", 0)
            try:
                retry_after = float(retry_after_raw)
            except (TypeError, ValueError):
                retry_after = 0.0
            raise LeverRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise LeverNetworkError(
                f"Lever server error {status}: {err_msg}",
                status_code=status,
            )
        raise LeverError(
            f"Lever error {status}: {err_msg}", status_code=status
        )

    # ── Paginated list helper ─────────────────────────────────────────────────

    async def _get_list(
        self,
        path: str,
        limit: int = 100,
        cursor: str | None = None,
        extra_params: dict[str, Any] | None = None,
        api_key: str = "",
    ) -> dict[str, Any]:
        """Fetch a single page from a Lever list endpoint.

        Returns the raw Lever response: {"data": [...], "hasNext": bool, "next": "cursor"}
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if extra_params:
            params.update(extra_params)
        url = f"{LEVER_API_BASE}/{path}"
        return await self._request("GET", url, params=params, api_key=api_key)

    # ── Public API methods ────────────────────────────────────────────────────

    async def get_users(
        self,
        limit: int = 100,
        cursor: str | None = None,
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /users — list all Lever users (team members).

        Returns: {"data": [...], "hasNext": bool, "next": "cursor"}
        """
        return await self._get_list("users", limit=limit, cursor=cursor, api_key=api_key)

    async def get_opportunities(
        self,
        limit: int = 100,
        cursor: str | None = None,
        api_key: str = "",
        **params: Any,
    ) -> dict[str, Any]:
        """GET /opportunities — list all candidates/opportunities.

        Returns: {"data": [...], "hasNext": bool, "next": "cursor"}
        Supports extra params: stage_id, tag, posting_id, etc.
        """
        return await self._get_list(
            "opportunities",
            limit=limit,
            cursor=cursor,
            extra_params=params or None,
            api_key=api_key,
        )

    async def get_postings(
        self,
        limit: int = 100,
        cursor: str | None = None,
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /postings — list all job postings.

        Returns: {"data": [...], "hasNext": bool, "next": "cursor"}
        """
        return await self._get_list("postings", limit=limit, cursor=cursor, api_key=api_key)

    async def get_interviews(
        self,
        limit: int = 100,
        cursor: str | None = None,
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /interviews — list all scheduled interviews.

        Returns: {"data": [...], "hasNext": bool, "next": "cursor"}
        """
        return await self._get_list("interviews", limit=limit, cursor=cursor, api_key=api_key)

    async def get_offers(
        self,
        opportunity_id: str,
        limit: int = 100,
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /opportunities/{id}/offers — list offers for a specific opportunity.

        Returns: {"data": [...], "hasNext": bool, "next": "cursor"}
        """
        url = f"{LEVER_API_BASE}/opportunities/{opportunity_id}/offers"
        params: dict[str, Any] = {"limit": limit}
        return await self._request("GET", url, params=params, api_key=api_key)

    async def get_opportunity(
        self,
        opportunity_id: str,
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /opportunities/{id} — retrieve a single opportunity by ID.

        Returns: {"data": {...}}
        """
        url = f"{LEVER_API_BASE}/opportunities/{opportunity_id}"
        return await self._request("GET", url, api_key=api_key)

    async def get_stages(
        self,
        api_key: str = "",
    ) -> dict[str, Any]:
        """GET /stages — list all pipeline stages.

        Returns: {"data": [...]}
        """
        url = f"{LEVER_API_BASE}/stages"
        return await self._request("GET", url, api_key=api_key)
