from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import ShopifyHTTPClient
from exceptions import ShopifyAuthError, ShopifyError
from helpers import normalize_customer, normalize_order, normalize_product, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

try:
    from shared.base_connector import BaseConnector
    _BASE = BaseConnector
except ImportError:
    _BASE = object  # standalone / test mode

SYNC_PAGE_SIZE = 100


class ShopifyConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Shopify.

    Provides authentication, health checks, full/incremental sync, and
    direct access to Shopify orders, products, and customers via the
    Shopify Admin REST API 2024-01.
    """

    CONNECTOR_TYPE: str = "shopify"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
        # Standalone / test convenience args
        shop_url: str = "",
        access_token: str = "",
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        # Accept either `shop_domain` (spec / ACP install_fields key) or
        # `shop_url` (legacy / standalone convenience arg).
        self._shop_url: str = (
            _config.get("shop_domain", "")
            or _config.get("shop_url", "")
            or shop_url
        )
        self._access_token: str = _config.get("access_token", "") or access_token
        self.http_client: ShopifyHTTPClient | None = None

    def _make_client(self) -> ShopifyHTTPClient:
        return ShopifyHTTPClient()

    def _ensure_client(self) -> ShopifyHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling GET /shop.json."""
        if not self._shop_url or not self._access_token:
            missing = []
            if not self._shop_url:
                missing.append("shop_domain")
            if not self._access_token:
                missing.append("access_token")
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_shop, self._shop_url, self._access_token
            )
            await client.aclose()
            shop = data.get("shop", {})
            shop_name = shop.get("name", self._shop_url)
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or str(shop.get("id", "")),
                message=f"Connected to Shopify store: {shop_name}",
            )
        except ShopifyAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /shop.json and return current health."""
        if not self._shop_url or not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="shop_domain and access_token are required",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_shop, self._shop_url, self._access_token
            )
            await client.aclose()
            shop_name = data.get("shop", {}).get("name", self._shop_url)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Shopify store: {shop_name}",
            )
        except ShopifyAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync orders, products, and customers from Shopify.

        full=True → fetch all records.
        since=<datetime> → fetch records created after that timestamp (ISO 8601).
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        created_at_min: str | None = None
        if not full and since:
            created_at_min = since.isoformat()

        found = 0
        synced = 0
        failed = 0

        # Sync orders
        try:
            page_info: str | None = None
            while True:
                orders, next_page_info = await with_retry(
                    self.http_client.list_orders,
                    self._shop_url,
                    self._access_token,
                    SYNC_PAGE_SIZE,
                    page_info,
                    "any",
                    created_at_min,
                )
                found += len(orders)
                for order in orders:
                    try:
                        doc = normalize_order(order, self.connector_id, self._tenant_id, self._shop_url)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if not next_page_info:
                    break
                page_info = next_page_info
        except ShopifyError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Orders sync failed: {exc}",
            )

        # Sync products
        try:
            page_info = None
            while True:
                products, next_page_info = await with_retry(
                    self.http_client.list_products,
                    self._shop_url,
                    self._access_token,
                    SYNC_PAGE_SIZE,
                    page_info,
                    "any",
                )
                found += len(products)
                for product in products:
                    try:
                        doc = normalize_product(product, self.connector_id, self._tenant_id, self._shop_url)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if not next_page_info:
                    break
                page_info = next_page_info
        except ShopifyError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Products sync failed: {exc}",
            )

        # Sync customers
        try:
            page_info = None
            while True:
                customers, next_page_info = await with_retry(
                    self.http_client.list_customers,
                    self._shop_url,
                    self._access_token,
                    SYNC_PAGE_SIZE,
                    page_info,
                    created_at_min,
                )
                found += len(customers)
                for customer in customers:
                    try:
                        doc = normalize_customer(customer, self.connector_id, self._tenant_id, self._shop_url)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if not next_page_info:
                    break
                page_info = next_page_info
        except ShopifyError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Customers sync failed: {exc}",
            )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Orders ───────────────────────────────────────────────────────────────

    async def list_orders(
        self,
        limit: int = 100,
        status: str = "any",
        created_at_min: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all orders, following cursor pagination automatically."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        page_info: str | None = None
        while True:
            page, next_page_info = await with_retry(
                client.list_orders,
                self._shop_url,
                self._access_token,
                limit,
                page_info,
                status,
                created_at_min,
            )
            results.extend(page)
            if not next_page_info:
                break
            page_info = next_page_info
        return results

    async def get_order(self, order_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_order, self._shop_url, self._access_token, order_id)

    # ── Products ─────────────────────────────────────────────────────────────

    async def list_products(
        self,
        limit: int = 100,
        published_status: str = "any",
    ) -> list[dict[str, Any]]:
        """Return all products, following cursor pagination automatically."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        page_info: str | None = None
        while True:
            page, next_page_info = await with_retry(
                client.list_products,
                self._shop_url,
                self._access_token,
                limit,
                page_info,
                published_status,
            )
            results.extend(page)
            if not next_page_info:
                break
            page_info = next_page_info
        return results

    async def get_product(self, product_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_product, self._shop_url, self._access_token, product_id)

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(
        self,
        limit: int = 100,
        created_at_min: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all customers, following cursor pagination automatically."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        page_info: str | None = None
        while True:
            page, next_page_info = await with_retry(
                client.list_customers,
                self._shop_url,
                self._access_token,
                limit,
                page_info,
                created_at_min,
            )
            results.extend(page)
            if not next_page_info:
                break
            page_info = next_page_info
        return results

    async def get_customer(self, customer_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_customer, self._shop_url, self._access_token, customer_id)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> ShopifyConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
