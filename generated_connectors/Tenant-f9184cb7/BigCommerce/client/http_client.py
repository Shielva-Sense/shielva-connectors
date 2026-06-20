from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    BigCommerceAuthError,
    BigCommerceNetworkError,
    BigCommerceNotFoundError,
    BigCommerceRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0


class BigCommerceHTTPClient:
    """Low-level async HTTP client for the BigCommerce REST API (v2/v3)."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _base_url(self, store_hash: str) -> str:
        return f"https://api.bigcommerce.com/stores/{store_hash}"

    def _headers(self, access_token: str) -> dict[str, str]:
        return {
            "X-Auth-Token": access_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        store_hash: str,
        access_token: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a request and return the parsed JSON body."""
        url = f"{self._base_url(store_hash)}{path}"
        headers = self._headers(access_token)
        session = self._get_session()
        try:
            async with session.request(
                method, url, headers=headers, params=params
            ) as response:
                if response.status in (200, 201, 204):
                    if response.status == 204:
                        return {}
                    body: dict[str, Any] = await response.json(content_type=None)
                    return body

                body_text = await response.text()
                if response.status in (401, 403):
                    raise BigCommerceAuthError(
                        f"BigCommerce auth error {response.status}: {body_text}",
                        status_code=response.status,
                        code="invalid_credentials",
                    )
                if response.status == 404:
                    raise BigCommerceNotFoundError("resource", path)
                if response.status == 429:
                    retry_after = float(
                        response.headers.get("X-Rate-Limit-Time-Reset-Ms", "2000")
                    ) / 1000.0
                    raise BigCommerceRateLimitError(
                        f"Rate limited by BigCommerce: {body_text}",
                        retry_after=retry_after,
                    )
                if response.status >= 500:
                    raise BigCommerceNetworkError(
                        f"BigCommerce server error {response.status}: {body_text}",
                        status_code=response.status,
                    )
                raise BigCommerceNetworkError(
                    f"Unexpected status {response.status}: {body_text}",
                    status_code=response.status,
                )
        except (
            BigCommerceAuthError,
            BigCommerceNotFoundError,
            BigCommerceRateLimitError,
            BigCommerceNetworkError,
        ):
            raise
        except aiohttp.ClientConnectionError as exc:
            raise BigCommerceNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise BigCommerceNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise BigCommerceNetworkError(f"Unexpected error: {exc}") from exc

    # ── Store ─────────────────────────────────────────────────────────────────

    async def get_store(self, store_hash: str, access_token: str) -> dict[str, Any]:
        """GET /v2/store — verify credentials and return store info."""
        return await self._request("GET", store_hash, access_token, "/v2/store")

    # ── Products (v3) ─────────────────────────────────────────────────────────

    async def list_products(
        self,
        store_hash: str,
        access_token: str,
        limit: int = 250,
        page: int = 1,
    ) -> tuple[list[dict[str, Any]], int]:
        """GET /v3/catalog/products — returns (products, total_pages)."""
        params: dict[str, Any] = {"limit": limit, "page": page}
        body = await self._request(
            "GET", store_hash, access_token, "/v3/catalog/products", params=params
        )
        products: list[dict[str, Any]] = body.get("data", [])
        meta = body.get("meta", {})
        pagination = meta.get("pagination", {})
        total_pages: int = int(pagination.get("total_pages", 1))
        return products, total_pages

    async def get_product(
        self, store_hash: str, access_token: str, product_id: int | str
    ) -> dict[str, Any]:
        """GET /v3/catalog/products/{id} — return a single product."""
        body = await self._request(
            "GET", store_hash, access_token, f"/v3/catalog/products/{product_id}"
        )
        return body.get("data", body)

    # ── Orders (v2) ──────────────────────────────────────────────────────────

    async def list_orders(
        self,
        store_hash: str,
        access_token: str,
        limit: int = 250,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """GET /v2/orders — returns orders list (v2 returns array directly)."""
        params: dict[str, Any] = {"limit": limit, "page": page}
        body = await self._request(
            "GET", store_hash, access_token, "/v2/orders", params=params
        )
        # v2 returns a list directly (not wrapped in data key)
        if isinstance(body, list):
            return body  # type: ignore[return-value]
        return body.get("data", [])

    async def get_order(
        self, store_hash: str, access_token: str, order_id: int | str
    ) -> dict[str, Any]:
        """GET /v2/orders/{id} — return a single order."""
        body = await self._request(
            "GET", store_hash, access_token, f"/v2/orders/{order_id}"
        )
        return body

    # ── Customers (v3) ───────────────────────────────────────────────────────

    async def list_customers(
        self,
        store_hash: str,
        access_token: str,
        limit: int = 250,
        page: int = 1,
    ) -> tuple[list[dict[str, Any]], int]:
        """GET /v3/customers — returns (customers, total_pages)."""
        params: dict[str, Any] = {"limit": limit, "page": page}
        body = await self._request(
            "GET", store_hash, access_token, "/v3/customers", params=params
        )
        customers: list[dict[str, Any]] = body.get("data", [])
        meta = body.get("meta", {})
        pagination = meta.get("pagination", {})
        total_pages: int = int(pagination.get("total_pages", 1))
        return customers, total_pages

    async def get_customer(
        self, store_hash: str, access_token: str, customer_id: int | str
    ) -> dict[str, Any]:
        """GET /v3/customers?id:in={id} — return a single customer."""
        params: dict[str, Any] = {"id:in": str(customer_id)}
        body = await self._request(
            "GET", store_hash, access_token, "/v3/customers", params=params
        )
        data: list[dict[str, Any]] = body.get("data", [])
        if not data:
            raise BigCommerceNotFoundError("customer", str(customer_id))
        return data[0]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> BigCommerceHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
