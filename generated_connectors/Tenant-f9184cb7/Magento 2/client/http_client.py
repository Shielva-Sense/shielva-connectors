from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    MagentoAuthError,
    MagentoNetworkError,
    MagentoNotFoundError,
    MagentoRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0


class MagentoHTTPClient:
    """Low-level async HTTP client for the Magento 2 REST API."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _api_base(self, base_url: str) -> str:
        host = base_url.rstrip("/")
        if not host.startswith("http"):
            host = f"https://{host}"
        return f"{host}/rest/V1"

    async def _request(
        self,
        method: str,
        base_url: str,
        access_token: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        """Execute a request and return the parsed JSON body."""
        url = f"{self._api_base(base_url)}{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        session = self._get_session()
        try:
            async with session.request(
                method, url, headers=headers, params=params
            ) as response:
                if response.status == 200:
                    body: dict[str, Any] | list[Any] = await response.json(content_type=None)
                    return body

                body_text = await response.text()
                resp_headers = dict(response.headers)

                if response.status in (401, 403):
                    raise MagentoAuthError(
                        f"Magento auth error {response.status}: {body_text}",
                        status_code=response.status,
                        code="invalid_credentials",
                    )
                if response.status == 404:
                    raise MagentoNotFoundError("resource", path)
                if response.status == 429:
                    retry_after = float(resp_headers.get("Retry-After", "2"))
                    raise MagentoRateLimitError(
                        f"Rate limited by Magento: {body_text}",
                        retry_after=retry_after,
                    )
                if response.status >= 500:
                    raise MagentoNetworkError(
                        f"Magento server error {response.status}: {body_text}",
                        status_code=response.status,
                    )
                raise MagentoNetworkError(
                    f"Unexpected status {response.status}: {body_text}",
                    status_code=response.status,
                )
        except (MagentoAuthError, MagentoNotFoundError, MagentoRateLimitError, MagentoNetworkError):
            raise
        except aiohttp.ClientConnectionError as exc:
            raise MagentoNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise MagentoNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise MagentoNetworkError(f"Unexpected error: {exc}") from exc

    # ── Store ─────────────────────────────────────────────────────────────────

    async def get_store_info(
        self, base_url: str, access_token: str
    ) -> list[Any]:
        """GET /store/storeConfigs — verify credentials and return store configs."""
        result = await self._request("GET", base_url, access_token, "/store/storeConfigs")
        return result if isinstance(result, list) else [result]

    # ── Orders ────────────────────────────────────────────────────────────────

    async def list_orders(
        self,
        base_url: str,
        access_token: str,
        page: int = 1,
        page_size: int = 100,
        sort_field: str = "created_at",
        sort_direction: str = "DESC",
        created_after: str | None = None,
    ) -> dict[str, Any]:
        """GET /orders — returns paginated orders with total_count."""
        params: dict[str, Any] = {
            "searchCriteria[pageSize]": page_size,
            "searchCriteria[currentPage]": page,
            "searchCriteria[sortOrders][0][field]": sort_field,
            "searchCriteria[sortOrders][0][direction]": sort_direction,
        }
        if created_after:
            params["searchCriteria[filterGroups][0][filters][0][field]"] = "created_at"
            params["searchCriteria[filterGroups][0][filters][0][value]"] = created_after
            params["searchCriteria[filterGroups][0][filters][0][conditionType]"] = "gt"

        result = await self._request("GET", base_url, access_token, "/orders", params=params)
        return result if isinstance(result, dict) else {"items": result, "total_count": len(result)}

    async def get_order(
        self, base_url: str, access_token: str, order_id: int | str
    ) -> dict[str, Any]:
        """GET /orders/{id} — return a single order."""
        result = await self._request("GET", base_url, access_token, f"/orders/{order_id}")
        return result if isinstance(result, dict) else {}

    # ── Products ──────────────────────────────────────────────────────────────

    async def list_products(
        self,
        base_url: str,
        access_token: str,
        page: int = 1,
        page_size: int = 100,
        created_after: str | None = None,
    ) -> dict[str, Any]:
        """GET /products — returns paginated products with total_count."""
        params: dict[str, Any] = {
            "searchCriteria[pageSize]": page_size,
            "searchCriteria[currentPage]": page,
        }
        if created_after:
            params["searchCriteria[filterGroups][0][filters][0][field]"] = "created_at"
            params["searchCriteria[filterGroups][0][filters][0][value]"] = created_after
            params["searchCriteria[filterGroups][0][filters][0][conditionType]"] = "gt"

        result = await self._request("GET", base_url, access_token, "/products", params=params)
        return result if isinstance(result, dict) else {"items": result, "total_count": len(result)}

    async def get_product(
        self, base_url: str, access_token: str, sku: str
    ) -> dict[str, Any]:
        """GET /products/{sku} — return a single product."""
        result = await self._request("GET", base_url, access_token, f"/products/{sku}")
        return result if isinstance(result, dict) else {}

    # ── Customers ─────────────────────────────────────────────────────────────

    async def list_customers(
        self,
        base_url: str,
        access_token: str,
        page: int = 1,
        page_size: int = 100,
        created_after: str | None = None,
    ) -> dict[str, Any]:
        """GET /customers/search — returns paginated customers with total_count."""
        params: dict[str, Any] = {
            "searchCriteria[pageSize]": page_size,
            "searchCriteria[currentPage]": page,
        }
        if created_after:
            params["searchCriteria[filterGroups][0][filters][0][field]"] = "created_at"
            params["searchCriteria[filterGroups][0][filters][0][value]"] = created_after
            params["searchCriteria[filterGroups][0][filters][0][conditionType]"] = "gt"

        result = await self._request("GET", base_url, access_token, "/customers/search", params=params)
        return result if isinstance(result, dict) else {"items": result, "total_count": len(result)}

    async def get_customer(
        self, base_url: str, access_token: str, customer_id: int | str
    ) -> dict[str, Any]:
        """GET /customers/{id} — return a single customer."""
        result = await self._request("GET", base_url, access_token, f"/customers/{customer_id}")
        return result if isinstance(result, dict) else {}

    # ── Categories ────────────────────────────────────────────────────────────

    async def list_categories(
        self, base_url: str, access_token: str
    ) -> dict[str, Any]:
        """GET /categories — return full category tree."""
        result = await self._request("GET", base_url, access_token, "/categories")
        return result if isinstance(result, dict) else {"id": 0, "children_data": result}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> MagentoHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
