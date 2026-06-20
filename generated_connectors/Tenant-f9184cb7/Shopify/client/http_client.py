from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from exceptions import (
    ShopifyAuthError,
    ShopifyNetworkError,
    ShopifyNotFoundError,
    ShopifyRateLimitError,
)

SHOPIFY_API_VERSION = "2024-01"
DEFAULT_TIMEOUT_S = 30.0


def _parse_next_page_info(link_header: str | None) -> str | None:
    """Extract page_info for the next page from a Shopify Link header.

    The Link header looks like:
      <https://mystore.myshopify.com/admin/api/2024-01/orders.json?page_info=abc&limit=100>; rel="next"
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            match = re.search(r"<([^>]+)>", part)
            if match:
                url = match.group(1)
                qs = parse_qs(urlparse(url).query)
                page_infos = qs.get("page_info", [])
                if page_infos:
                    return page_infos[0]
    return None


class ShopifyHTTPClient:
    """Low-level async HTTP client for the Shopify Admin REST API."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _base_url(self, shop_url: str) -> str:
        host = shop_url.rstrip("/")
        if not host.startswith("http"):
            host = f"https://{host}"
        return f"{host}/admin/api/{SHOPIFY_API_VERSION}"

    async def _request(
        self,
        method: str,
        shop_url: str,
        access_token: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Execute a request and return (body, response_headers)."""
        url = f"{self._base_url(shop_url)}{path}"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        session = self._get_session()
        try:
            async with session.request(
                method, url, headers=headers, params=params
            ) as response:
                resp_headers = dict(response.headers)

                if response.status == 200:
                    body: dict[str, Any] = await response.json(content_type=None)
                    return body, resp_headers

                body_text = await response.text()
                if response.status in (401, 403):
                    raise ShopifyAuthError(
                        f"Shopify auth error {response.status}: {body_text}",
                        status_code=response.status,
                        code="invalid_credentials",
                    )
                if response.status == 404:
                    raise ShopifyNotFoundError("resource", path)
                if response.status == 429:
                    retry_after = float(resp_headers.get("Retry-After", "2"))
                    raise ShopifyRateLimitError(
                        f"Rate limited by Shopify: {body_text}",
                        retry_after=retry_after,
                    )
                if response.status >= 500:
                    raise ShopifyNetworkError(
                        f"Shopify server error {response.status}: {body_text}",
                        status_code=response.status,
                    )
                raise ShopifyNetworkError(
                    f"Unexpected status {response.status}: {body_text}",
                    status_code=response.status,
                )
        except (ShopifyAuthError, ShopifyNotFoundError, ShopifyRateLimitError, ShopifyNetworkError):
            raise
        except aiohttp.ClientConnectionError as exc:
            raise ShopifyNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise ShopifyNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise ShopifyNetworkError(f"Unexpected error: {exc}") from exc

    # ── Shop ─────────────────────────────────────────────────────────────────

    async def get_shop(self, shop_url: str, access_token: str) -> dict[str, Any]:
        """GET /shop.json — verify credentials and return shop info."""
        body, _ = await self._request("GET", shop_url, access_token, "/shop.json")
        return body

    # ── Orders ───────────────────────────────────────────────────────────────

    async def list_orders(
        self,
        shop_url: str,
        access_token: str,
        limit: int = 100,
        page_info: str | None = None,
        status: str = "any",
        created_at_min: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /orders.json — returns (orders, next_page_info)."""
        params: dict[str, Any] = {"limit": limit}
        if page_info:
            # Shopify cursor pagination: when page_info is set, only limit is allowed
            params = {"limit": limit, "page_info": page_info}
        else:
            params["status"] = status
            if created_at_min:
                params["created_at_min"] = created_at_min
        body, headers = await self._request("GET", shop_url, access_token, "/orders.json", params=params)
        orders: list[dict[str, Any]] = body.get("orders", [])
        next_page_info = _parse_next_page_info(headers.get("Link"))
        return orders, next_page_info

    async def get_order(
        self, shop_url: str, access_token: str, order_id: int | str
    ) -> dict[str, Any]:
        """GET /orders/{id}.json — return a single order."""
        body, _ = await self._request("GET", shop_url, access_token, f"/orders/{order_id}.json")
        return body

    # ── Products ─────────────────────────────────────────────────────────────

    async def list_products(
        self,
        shop_url: str,
        access_token: str,
        limit: int = 100,
        page_info: str | None = None,
        published_status: str = "any",
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /products.json — returns (products, next_page_info)."""
        params: dict[str, Any] = {"limit": limit}
        if page_info:
            params = {"limit": limit, "page_info": page_info}
        else:
            params["published_status"] = published_status
        body, headers = await self._request("GET", shop_url, access_token, "/products.json", params=params)
        products: list[dict[str, Any]] = body.get("products", [])
        next_page_info = _parse_next_page_info(headers.get("Link"))
        return products, next_page_info

    async def get_product(
        self, shop_url: str, access_token: str, product_id: int | str
    ) -> dict[str, Any]:
        """GET /products/{id}.json — return a single product."""
        body, _ = await self._request("GET", shop_url, access_token, f"/products/{product_id}.json")
        return body

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(
        self,
        shop_url: str,
        access_token: str,
        limit: int = 100,
        page_info: str | None = None,
        created_at_min: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /customers.json — returns (customers, next_page_info)."""
        params: dict[str, Any] = {"limit": limit}
        if page_info:
            params = {"limit": limit, "page_info": page_info}
        else:
            if created_at_min:
                params["created_at_min"] = created_at_min
        body, headers = await self._request("GET", shop_url, access_token, "/customers.json", params=params)
        customers: list[dict[str, Any]] = body.get("customers", [])
        next_page_info = _parse_next_page_info(headers.get("Link"))
        return customers, next_page_info

    async def get_customer(
        self, shop_url: str, access_token: str, customer_id: int | str
    ) -> dict[str, Any]:
        """GET /customers/{id}.json — return a single customer."""
        body, _ = await self._request("GET", shop_url, access_token, f"/customers/{customer_id}.json")
        return body

    # ── Collections ──────────────────────────────────────────────────────────

    async def list_collections(
        self,
        shop_url: str,
        access_token: str,
        limit: int = 250,
        page_info: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /custom_collections.json — returns (collections, next_page_info)."""
        params: dict[str, Any] = {"limit": limit}
        if page_info:
            params = {"limit": limit, "page_info": page_info}
        body, headers = await self._request("GET", shop_url, access_token, "/custom_collections.json", params=params)
        collections: list[dict[str, Any]] = body.get("custom_collections", [])
        next_page_info = _parse_next_page_info(headers.get("Link"))
        return collections, next_page_info

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> ShopifyHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
