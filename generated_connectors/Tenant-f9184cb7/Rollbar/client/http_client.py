from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    RollbarAuthError,
    RollbarError,
    RollbarNetworkError,
    RollbarNotFoundError,
    RollbarRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_BASE_URL: str = "https://api.rollbar.com"


class RollbarHTTPClient:
    """Low-level async HTTP client for the Rollbar REST API v1.

    Auth: ``?access_token={token}`` query parameter (NOT Bearer header).
    Base: ``https://api.rollbar.com``
    Pagination: offset-based via ``page`` parameter.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._access_token: str = cfg.get("access_token", "")
        base = (cfg.get("base_url", "") or DEFAULT_BASE_URL).rstrip("/")
        self._base_url: str = base
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _auth_params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build query params dict with access_token injected."""
        params: dict[str, Any] = {"access_token": self._access_token}
        if extra:
            params.update(extra)
        return params

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Perform an HTTP request and return the parsed JSON body."""
        url = f"{self._base_url}{path}"
        request_params = self._auth_params(params)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, params=request_params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise RollbarNetworkError(f"Network error: {exc}") from exc
        except (
            RollbarError,
            RollbarAuthError,
            RollbarRateLimitError,
            RollbarNotFoundError,
            RollbarNetworkError,
        ):
            raise
        except Exception as exc:
            raise RollbarNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(self, response: aiohttp.ClientResponse) -> Any:
        status = response.status

        if status in (200, 201):
            try:
                return await response.json()
            except Exception:
                return await response.text()

        body: Any = {}
        try:
            body = await response.json()
        except Exception:
            pass

        self._raise_for_status(status, body)
        raise RollbarError(f"HTTP {status}", status_code=status)

    def _raise_for_status(self, status: int, body: Any) -> None:
        """Map HTTP status codes to typed Rollbar exceptions."""
        if isinstance(body, dict):
            err_msg: str = (
                body.get("message", "")
                or body.get("err", "")
                or f"HTTP {status}"
            )
        else:
            err_msg = str(body) if body else f"HTTP {status}"

        if status in (401, 403):
            raise RollbarAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise RollbarNotFoundError("resource", err_msg)
        if status == 429:
            raise RollbarRateLimitError(
                f"Rate limited: {err_msg}", retry_after=0.0
            )
        if status >= 500:
            raise RollbarNetworkError(
                f"Rollbar server error {status}: {err_msg}",
                status_code=status,
            )
        raise RollbarError(
            f"Rollbar error {status}: {err_msg}", status_code=status
        )

    # ── Project ───────────────────────────────────────────────────────────────

    async def get_project(self) -> dict[str, Any]:
        """GET /api/1/project/ — fetch project info (validates access_token)."""
        body = await self._request("GET", "/api/1/project/")
        result: dict[str, Any] = body.get("result", {}) if isinstance(body, dict) else {}
        return result

    # ── Project users ─────────────────────────────────────────────────────────

    async def get_project_users(self) -> list[dict[str, Any]]:
        """GET /api/1/project/users/ — fetch all project members."""
        body = await self._request("GET", "/api/1/project/users/")
        result = body.get("result", {}) if isinstance(body, dict) else {}
        users: list[dict[str, Any]] = result.get("users", []) if isinstance(result, dict) else []
        return users

    # ── Items (error groups) ──────────────────────────────────────────────────

    async def get_items(
        self,
        page: int = 1,
        level: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/1/items/ — fetch a page of error items.

        Returns the full response dict (result.items + result.total_count).
        """
        params: dict[str, Any] = {"page": page}
        if level:
            params["level"] = level
        if status:
            params["status"] = status
        body = await self._request("GET", "/api/1/items/", params=params)
        return body if isinstance(body, dict) else {}

    async def get_item(self, item_id: int | str) -> dict[str, Any]:
        """GET /api/1/item/{item_id}/ — fetch a single item by ID."""
        body = await self._request("GET", f"/api/1/item/{item_id}/")
        result: dict[str, Any] = body.get("result", {}) if isinstance(body, dict) else {}
        return result

    # ── Occurrences (raw instances) ───────────────────────────────────────────

    async def get_occurrences(self, page: int = 1) -> dict[str, Any]:
        """GET /api/1/instances/ — fetch a page of raw error occurrences."""
        body = await self._request("GET", "/api/1/instances/", params={"page": page})
        return body if isinstance(body, dict) else {}

    # ── Deploys ───────────────────────────────────────────────────────────────

    async def get_deploys(self, page: int = 1) -> dict[str, Any]:
        """GET /api/1/deploys/ — fetch a page of deploys."""
        body = await self._request("GET", "/api/1/deploys/", params={"page": page})
        return body if isinstance(body, dict) else {}

    # ── Reports ───────────────────────────────────────────────────────────────

    async def get_top_active_items(self) -> dict[str, Any]:
        """GET /api/1/reports/top_active_items/ — fetch top active error items."""
        body = await self._request("GET", "/api/1/reports/top_active_items/")
        return body if isinstance(body, dict) else {}
