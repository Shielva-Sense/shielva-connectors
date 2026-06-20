from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    RipplingAuthError,
    RipplingError,
    RipplingNetworkError,
    RipplingNotFoundError,
    RipplingRateLimitError,
)

BASE_URL: str = "https://api.rippling.com/platform/api"
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_LIMIT: int = 100


class RipplingHTTPClient:
    """Low-level async HTTP client for the Rippling Platform API.

    Authentication: Bearer token in the Authorization header.
    Pagination: cursor-based using offset + limit; response wraps items under
    a ``data`` key with optional ``next_cursor`` and ``total`` fields.
    Falls back gracefully if the response is a bare list instead of a dict.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._config: dict[str, Any] = config or {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _api_key(self) -> str:
        return self._config.get("api_key", "")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}{path}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                ) as response:
                    return await self._raise_for_status(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise RipplingNetworkError(f"Network error: {exc}") from exc
        except (
            RipplingError,
            RipplingAuthError,
            RipplingRateLimitError,
            RipplingNotFoundError,
            RipplingNetworkError,
        ):
            raise
        except Exception as exc:
            raise RipplingNetworkError(f"Unexpected network error: {exc}") from exc

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            try:
                raw = await response.json(content_type=None)
            except Exception:
                return {}
            # Normalise: always return dict with a "data" key
            if isinstance(raw, list):
                return {"data": raw, "total": len(raw)}
            return raw

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_msg: str = (
            body.get("message", "")
            or body.get("error", "")
            or body.get("detail", "")
            or f"HTTP {status}"
        )

        if status in (401, 403):
            raise RipplingAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise RipplingNotFoundError("resource", err_msg)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise RipplingRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise RipplingNetworkError(
                f"Rippling server error {status}: {err_msg}",
                status_code=status,
            )
        raise RipplingError(
            f"Rippling error {status}: {err_msg}", status_code=status
        )

    # ── Company ───────────────────────────────────────────────────────────────

    async def get_company(self) -> dict[str, Any]:
        """GET /companies — return the authenticated company record."""
        result = await self._request("GET", "/companies")
        # Rippling may return a list or a dict; normalise to first element
        data = result.get("data", result)
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    # ── Employees ─────────────────────────────────────────────────────────────

    async def list_employees(
        self,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """GET /employees — paginated employee list (cursor-based)."""
        params: dict[str, Any] = {"limit": limit, "offset": 0}
        if cursor is not None:
            # Rippling uses offset as the cursor
            try:
                params["offset"] = int(cursor)
            except (TypeError, ValueError):
                params["offset"] = 0
        return await self._request("GET", "/employees", params=params)

    # ── Departments ───────────────────────────────────────────────────────────

    async def list_departments(self) -> dict[str, Any]:
        """GET /departments — list all departments."""
        return await self._request("GET", "/departments")

    # ── Teams ─────────────────────────────────────────────────────────────────

    async def list_teams(self) -> dict[str, Any]:
        """GET /teams — list all teams."""
        return await self._request("GET", "/teams")

    # ── Roles ─────────────────────────────────────────────────────────────────

    async def list_roles(self) -> dict[str, Any]:
        """GET /roles — list all roles."""
        return await self._request("GET", "/roles")

    # ── Leaves ────────────────────────────────────────────────────────────────

    async def list_leaves(
        self,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """GET /leaves — paginated leave requests (cursor-based)."""
        params: dict[str, Any] = {"limit": limit, "offset": 0}
        if cursor is not None:
            try:
                params["offset"] = int(cursor)
            except (TypeError, ValueError):
                params["offset"] = 0
        return await self._request("GET", "/leaves", params=params)
