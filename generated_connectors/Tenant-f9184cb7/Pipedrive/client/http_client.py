from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    PipedriveAuthError,
    PipedriveError,
    PipedriveNetworkError,
    PipedriveNotFoundError,
    PipedriveRateLimitError,
    PipedriveServerError,
)

PIPEDRIVE_BASE_URL = "https://api.pipedrive.com/v1"
DEFAULT_TIMEOUT_S = 30.0


class PipedriveHTTPClient:
    """Low-level async HTTP client for the Pipedrive REST API v1.

    Authentication uses a Bearer token in the Authorization header.
    Pipedrive wraps all responses in {"success": true/false, "data": ...}
    and uses additional_data.pagination for paginated results.
    """

    def __init__(
        self,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=PIPEDRIVE_BASE_URL,
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        caller_params: dict[str, Any] = kwargs.pop("params", {}) or {}

        try:
            response = await self._client.request(
                method, path, params=caller_params, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise PipedriveNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise PipedriveNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            payload: dict[str, Any] = response.json()
            # Pipedrive returns {"success": false, ...} even on 200 for some errors
            if not payload.get("success", True):
                err_msg = payload.get("error", "Unknown Pipedrive error")
                err_code = payload.get("error_info", "")
                raise PipedriveError(f"Pipedrive API error: {err_msg}", 200, err_code)
            return payload

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        err_msg = (
            body.get("error")
            or body.get("message")
            or response.text
            or "Unknown Pipedrive error"
        )
        err_code = body.get("error_info", "")

        if response.status_code == 401:
            raise PipedriveAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if response.status_code == 403:
            raise PipedriveAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status_code == 404:
            raise PipedriveNotFoundError("resource", path)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise PipedriveRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise PipedriveServerError(
                f"Pipedrive server error {response.status_code}: {err_msg}",
                response.status_code,
            )

        raise PipedriveError(
            f"Pipedrive error {response.status_code}: {err_msg}",
            response.status_code,
            err_code,
        )

    # ── Auth probe ───────────────────────────────────────────────────────────

    async def get_current_user(self) -> dict[str, Any]:
        """GET /users/me — used for install/health-check."""
        return await self._request("GET", "/users/me")

    # ── Deals ────────────────────────────────────────────────────────────────

    async def list_deals(
        self,
        status: str = "all",
        limit: int = 100,
        start: int = 0,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/deals",
            params={"status": status, "limit": limit, "start": start},
        )

    async def get_deal(self, deal_id: int | str) -> dict[str, Any]:
        return await self._request("GET", f"/deals/{deal_id}")

    # ── Persons ──────────────────────────────────────────────────────────────

    async def list_persons(
        self,
        limit: int = 100,
        start: int = 0,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/persons",
            params={"limit": limit, "start": start},
        )

    async def get_person(self, person_id: int | str) -> dict[str, Any]:
        return await self._request("GET", f"/persons/{person_id}")

    # ── Organizations ────────────────────────────────────────────────────────

    async def list_organizations(
        self,
        limit: int = 100,
        start: int = 0,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/organizations",
            params={"limit": limit, "start": start},
        )

    # ── Activities ───────────────────────────────────────────────────────────

    async def list_activities(
        self,
        limit: int = 100,
        start: int = 0,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/activities",
            params={"limit": limit, "start": start},
        )

    # ── Pipelines ────────────────────────────────────────────────────────────

    async def list_pipelines(self) -> dict[str, Any]:
        """GET /pipelines — returns all pipelines (no pagination)."""
        return await self._request("GET", "/pipelines")

    # ── Stages ───────────────────────────────────────────────────────────────

    async def list_stages(self) -> dict[str, Any]:
        """GET /stages — returns all stages across all pipelines."""
        return await self._request("GET", "/stages")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PipedriveHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
