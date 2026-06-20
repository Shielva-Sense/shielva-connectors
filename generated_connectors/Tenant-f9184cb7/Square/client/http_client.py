from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    SquareAuthError,
    SquareError,
    SquareNetworkError,
    SquareNotFoundError,
    SquareRateLimitError,
    SquareServerError,
)

SQUARE_BASE_URL = "https://connect.squareup.com/v2"
SQUARE_VERSION = "2024-01-17"
DEFAULT_TIMEOUT_S = 30.0


class SquareHTTPClient:
    """Low-level async HTTP client for the Square REST API v2."""

    def __init__(
        self,
        access_token: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._client = httpx.AsyncClient(
            base_url=SQUARE_BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Square-Version": SQUARE_VERSION,
                "Content-Type": "application/json",
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise SquareNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise SquareNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        # Square wraps errors in {"errors": [{"category": "...", "code": "...", "detail": "..."}]}
        errors: list[dict[str, Any]] = body.get("errors", [])
        first_err: dict[str, Any] = errors[0] if errors else {}
        err_msg = first_err.get("detail") or response.text or "Unknown Square error"
        err_code = first_err.get("code", "")

        if response.status_code == 401:
            raise SquareAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if response.status_code == 403:
            raise SquareAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status_code == 404:
            raise SquareNotFoundError(first_err.get("category", "resource"), path)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise SquareRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise SquareServerError(
                f"Square server error {response.status_code}: {err_msg}",
                response.status_code,
            )

        raise SquareError(
            f"Square error {response.status_code}: {err_msg}",
            response.status_code,
            err_code,
        )

    # ── Auth probe ───────────────────────────────────────────────────────────

    async def get_merchant(self) -> dict[str, Any]:
        """Probe endpoint used for install/health-check — GET /merchants/me."""
        return await self._request("GET", "/merchants/me")

    # ── Payments ─────────────────────────────────────────────────────────────

    async def list_payments(
        self,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/payments", params=params)

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/payments/{payment_id}")

    # ── Orders ───────────────────────────────────────────────────────────────

    async def list_orders(
        self,
        location_id: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "location_ids": [location_id],
            "limit": limit,
        }
        if cursor:
            payload["cursor"] = cursor
        return await self._request("POST", "/orders/search", json=payload)

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(
        self,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/customers", params=params)

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/customers/{customer_id}")

    # ── Catalog ──────────────────────────────────────────────────────────────

    async def list_catalog_items(
        self,
        cursor: str | None = None,
        types: str = "ITEM",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"types": types}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/catalog/list", params=params)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SquareHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
