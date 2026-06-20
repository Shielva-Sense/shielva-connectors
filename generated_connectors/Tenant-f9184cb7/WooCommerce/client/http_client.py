from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    WooCommerceAuthError,
    WooCommerceNetworkError,
    WooCommerceNotFoundError,
    WooCommerceRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_PER_PAGE = 100


def _normalize_site_url(site_url: str) -> str:
    """Strip protocol prefix and trailing slash, then prepend https://."""
    url = site_url.strip()
    # Strip any existing protocol
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    return "https://" + url.rstrip("/")


def _api_base(site_url: str) -> str:
    """Return the WooCommerce REST API v3 base URL for a given site."""
    return f"{_normalize_site_url(site_url)}/wp-json/wc/v3"


class WooCommerceHTTPClient:
    """Low-level async HTTP client for the WooCommerce REST API v3.

    Uses aiohttp.BasicAuth(consumer_key, consumer_secret) for HTTP Basic Auth.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(
        self,
        method: str,
        site_url: str,
        consumer_key: str,
        consumer_secret: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """Execute a request and return (parsed_body, response_headers)."""
        url = f"{_api_base(site_url)}{path}"
        auth = aiohttp.BasicAuth(consumer_key, consumer_secret)
        headers = {
            "Accept": "application/json",
            "User-Agent": "Shielva-WooCommerce-Connector/1.0.0",
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, params=params, headers=headers, auth=auth, ssl=False
                ) as response:
                    resp_headers = dict(response.headers)
                    status = response.status

                    if status == 200:
                        body = await response.json(content_type=None)
                        return body, resp_headers

                    # Try to parse error body for a human-readable message
                    try:
                        error_body: dict[str, Any] = await response.json(content_type=None)
                    except Exception:
                        error_body = {}

                    err_msg: str = (
                        (
                            error_body.get("message")
                            or (error_body.get("error") or {}).get("message", "")
                        )
                        if isinstance(error_body, dict)
                        else str(error_body)
                    ) or f"HTTP {status}"
                    err_code: str = (
                        error_body.get("code", "") if isinstance(error_body, dict) else ""
                    )

                    if status in (401, 403):
                        raise WooCommerceAuthError(
                            f"Authentication failed ({status}): {err_msg}",
                            status_code=status,
                            code=err_code,
                        )
                    if status == 404:
                        raise WooCommerceNotFoundError("resource", err_code or "unknown")
                    if status == 429:
                        retry_after = float(resp_headers.get("Retry-After", "0"))
                        raise WooCommerceRateLimitError(
                            f"Rate limited: {err_msg}", retry_after=retry_after
                        )
                    if status >= 500:
                        raise WooCommerceNetworkError(
                            f"WooCommerce server error {status}: {err_msg}",
                            status_code=status,
                            code=err_code,
                        )

                    raise WooCommerceNetworkError(
                        f"Unexpected HTTP {status}: {err_msg}",
                        status_code=status,
                        code=err_code,
                    )

        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise WooCommerceNetworkError(f"Network error: {exc}") from exc
        except (WooCommerceAuthError, WooCommerceNotFoundError, WooCommerceRateLimitError, WooCommerceNetworkError):
            raise
        except Exception as exc:
            raise WooCommerceNetworkError(f"Unexpected error: {exc}") from exc

    def _raise_for_status(self, response: Any, body: Any) -> None:
        """Map HTTP status codes to typed exceptions (used by callers if needed)."""
        status = response.status
        if status in (401, 403):
            raise WooCommerceAuthError(f"Auth error {status}", status_code=status)
        if status == 404:
            raise WooCommerceNotFoundError("resource", "unknown")
        if status == 429:
            raise WooCommerceRateLimitError("Rate limited")
        if status >= 500:
            raise WooCommerceNetworkError(f"Server error {status}", status_code=status)

    # ── System ────────────────────────────────────────────────────────────────

    async def get_system_status(
        self,
        site_url: str,
        consumer_key: str,
        consumer_secret: str,
    ) -> dict[str, Any]:
        """GET /system_status — verify credentials and retrieve store info."""
        body, _ = await self._request(
            "GET", site_url, consumer_key, consumer_secret, "/system_status"
        )
        return body  # type: ignore[return-value]

    # ── Orders ────────────────────────────────────────────────────────────────

    async def list_orders(
        self,
        site_url: str,
        consumer_key: str,
        consumer_secret: str,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        status: str = "any",
        modified_after: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """GET /orders — paginated list of orders."""
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "status": status,
        }
        if modified_after:
            params["modified_after"] = modified_after
        body, headers = await self._request(
            "GET", site_url, consumer_key, consumer_secret, "/orders", params=params
        )
        return body, headers  # type: ignore[return-value]

    async def get_order(
        self,
        site_url: str,
        consumer_key: str,
        consumer_secret: str,
        order_id: int,
    ) -> dict[str, Any]:
        """GET /orders/{id} — single order."""
        body, _ = await self._request(
            "GET", site_url, consumer_key, consumer_secret, f"/orders/{order_id}"
        )
        return body  # type: ignore[return-value]

    # ── Products ──────────────────────────────────────────────────────────────

    async def list_products(
        self,
        site_url: str,
        consumer_key: str,
        consumer_secret: str,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        status: str = "any",
        modified_after: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """GET /products — paginated list of products."""
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "status": status,
        }
        if modified_after:
            params["modified_after"] = modified_after
        body, headers = await self._request(
            "GET", site_url, consumer_key, consumer_secret, "/products", params=params
        )
        return body, headers  # type: ignore[return-value]

    async def get_product(
        self,
        site_url: str,
        consumer_key: str,
        consumer_secret: str,
        product_id: int,
    ) -> dict[str, Any]:
        """GET /products/{id} — single product."""
        body, _ = await self._request(
            "GET", site_url, consumer_key, consumer_secret, f"/products/{product_id}"
        )
        return body  # type: ignore[return-value]

    # ── Categories ────────────────────────────────────────────────────────────

    async def list_categories(
        self,
        site_url: str,
        consumer_key: str,
        consumer_secret: str,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """GET /products/categories — paginated list of product categories."""
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        body, headers = await self._request(
            "GET", site_url, consumer_key, consumer_secret, "/products/categories", params=params
        )
        return body, headers  # type: ignore[return-value]

    # ── Customers ─────────────────────────────────────────────────────────────

    async def list_customers(
        self,
        site_url: str,
        consumer_key: str,
        consumer_secret: str,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        modified_after: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """GET /customers — paginated list of customers."""
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        if modified_after:
            params["modified_after"] = modified_after
        body, headers = await self._request(
            "GET", site_url, consumer_key, consumer_secret, "/customers", params=params
        )
        return body, headers  # type: ignore[return-value]

    async def get_customer(
        self,
        site_url: str,
        consumer_key: str,
        consumer_secret: str,
        customer_id: int,
    ) -> dict[str, Any]:
        """GET /customers/{id} — single customer."""
        body, _ = await self._request(
            "GET", site_url, consumer_key, consumer_secret, f"/customers/{customer_id}"
        )
        return body  # type: ignore[return-value]
