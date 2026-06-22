"""All Wix API HTTP calls — zero business logic, zero normalization.

httpx async client. The Wix REST API expects:
  Authorization: <api_key>          (raw key — NO 'Bearer' prefix)
  wix-account-id: <account_id>
  wix-site-id:    <site_id>         (when the endpoint is site-scoped)
  Content-Type:   application/json

Retry on 429/5xx with exponential backoff.
"""
import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import WixAuthError, WixError, WixNetworkError, WixNotFound

logger = structlog.get_logger(__name__)

_WIX_BASE = "https://www.wixapis.com"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class WixHTTPClient:
    """Thin async HTTP client for the Wix REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        api_key: str = "",
        account_id: str = "",
        default_site_id: str = "",
        base_url: str = _WIX_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._api_key = api_key or ""
        self._account_id = account_id or ""
        self._default_site_id = default_site_id or ""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _headers(self, site_id: Optional[str] = None) -> Dict[str, str]:
        # Wix expects the RAW key in Authorization — no 'Bearer ' prefix.
        headers: Dict[str, str] = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }
        if self._account_id:
            headers["wix-account-id"] = self._account_id
        sid = site_id or self._default_site_id
        if sid:
            headers["wix-site-id"] = sid
        return headers

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            message = (
                body.get("message")
                or body.get("error")
                or body.get("details")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        if status == 401 or status == 403:
            raise WixAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 404:
            raise WixNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        raise WixError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {"raw": body},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        site_id: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers(site_id)

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "wix.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                await self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "wix.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise WixNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise WixNetworkError(str(last_exc)) from last_exc
        raise WixNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Site management ────────────────────────────────────────────────────

    async def list_sites(
        self,
        paging_limit: int = 10,
        paging_offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /site-list/v2/sites — list sites in the account."""
        params: Dict[str, Any] = {
            "paging.limit": paging_limit,
            "paging.offset": paging_offset,
        }
        return await self._request(
            "GET",
            "/site-list/v2/sites",
            params=params,
            context="list_sites",
        )

    async def get_site(self, site_id: str) -> Dict[str, Any]:
        """GET /site-list/v2/sites/{id}."""
        return await self._request(
            "GET",
            f"/site-list/v2/sites/{site_id}",
            context=f"get_site({site_id})",
        )

    # ── Stores: products ───────────────────────────────────────────────────

    async def list_products(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /stores-reader/v1/products/query — list/search products."""
        body: Dict[str, Any] = {"query": query or {}}
        if paging:
            body["query"]["paging"] = paging
        return await self._request(
            "POST",
            "/stores-reader/v1/products/query",
            site_id=site_id,
            json_body=body,
            context="list_products",
        )

    async def get_product(self, site_id: str, product_id: str) -> Dict[str, Any]:
        """GET /stores-reader/v1/products/{id}."""
        return await self._request(
            "GET",
            f"/stores-reader/v1/products/{product_id}",
            site_id=site_id,
            context=f"get_product({product_id})",
        )

    async def create_product(
        self,
        site_id: str,
        product: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /stores/v1/products."""
        return await self._request(
            "POST",
            "/stores/v1/products",
            site_id=site_id,
            json_body={"product": product},
            context="create_product",
        )

    async def update_product(
        self,
        site_id: str,
        product_id: str,
        product: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /stores/v1/products/{id}."""
        return await self._request(
            "PATCH",
            f"/stores/v1/products/{product_id}",
            site_id=site_id,
            json_body={"product": product},
            context=f"update_product({product_id})",
        )

    # ── Ecom: orders ───────────────────────────────────────────────────────

    async def list_orders(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
        filter: Optional[Dict[str, Any]] = None,
        sort: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /ecom/v1/orders/search."""
        search: Dict[str, Any] = {}
        if filter:
            search["filter"] = filter
        if sort:
            search["sort"] = sort
        if paging:
            search["cursorPaging"] = paging
        body = {"search": search}
        return await self._request(
            "POST",
            "/ecom/v1/orders/search",
            site_id=site_id,
            json_body=body,
            context="list_orders",
        )

    async def get_order(self, site_id: str, order_id: str) -> Dict[str, Any]:
        """GET /ecom/v1/orders/{id}."""
        return await self._request(
            "GET",
            f"/ecom/v1/orders/{order_id}",
            site_id=site_id,
            context=f"get_order({order_id})",
        )

    # ── Contacts ───────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /contacts/v4/contacts/query."""
        query: Dict[str, Any] = {}
        if filter:
            query["filter"] = filter
        if paging:
            query["paging"] = paging
        return await self._request(
            "POST",
            "/contacts/v4/contacts/query",
            site_id=site_id,
            json_body={"query": query},
            context="list_contacts",
        )

    async def create_contact(
        self,
        site_id: str,
        info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /contacts/v4/contacts."""
        return await self._request(
            "POST",
            "/contacts/v4/contacts",
            site_id=site_id,
            json_body={"info": info},
            context="create_contact",
        )

    # ── Members ────────────────────────────────────────────────────────────

    async def list_members(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /members/v1/members/query."""
        query: Dict[str, Any] = {}
        if paging:
            query["paging"] = paging
        return await self._request(
            "POST",
            "/members/v1/members/query",
            site_id=site_id,
            json_body={"query": query},
            context="list_members",
        )

    # ── Blog ───────────────────────────────────────────────────────────────

    async def list_blog_posts(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /blog/v3/posts/query."""
        query: Dict[str, Any] = {}
        if paging:
            query["paging"] = paging
        return await self._request(
            "POST",
            "/blog/v3/posts/query",
            site_id=site_id,
            json_body={"query": query},
            context="list_blog_posts",
        )

    # ── Bookings ───────────────────────────────────────────────────────────

    async def list_bookings(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /bookings/v2/bookings/query."""
        query: Dict[str, Any] = {}
        if paging:
            query["paging"] = paging
        return await self._request(
            "POST",
            "/bookings/v2/bookings/query",
            site_id=site_id,
            json_body={"query": query},
            context="list_bookings",
        )

    # ── Subscriptions ──────────────────────────────────────────────────────

    async def list_subscriptions(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /subscriptions/v1/subscriptions/query."""
        query: Dict[str, Any] = {}
        if paging:
            query["paging"] = paging
        return await self._request(
            "POST",
            "/subscriptions/v1/subscriptions/query",
            site_id=site_id,
            json_body={"query": query},
            context="list_subscriptions",
        )
