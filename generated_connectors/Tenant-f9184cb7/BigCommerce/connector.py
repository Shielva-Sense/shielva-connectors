from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import BigCommerceHTTPClient
from exceptions import BigCommerceAuthError, BigCommerceError
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

SYNC_PAGE_SIZE = 250


class BigCommerceConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for BigCommerce.

    Provides authentication, health checks, full sync, and direct access to
    BigCommerce products (v3), orders (v2), and customers (v3) via the
    BigCommerce REST API.
    """

    CONNECTOR_TYPE: str = "bigcommerce"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
        # Standalone / test convenience args
        store_hash: str = "",
        access_token: str = "",
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        self._store_hash: str = _config.get("store_hash", "") or store_hash
        self._access_token: str = _config.get("access_token", "") or access_token
        self.http_client: BigCommerceHTTPClient | None = None

    def _make_client(self) -> BigCommerceHTTPClient:
        return BigCommerceHTTPClient()

    def _ensure_client(self) -> BigCommerceHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling GET /v2/store."""
        if not self._store_hash or not self._access_token:
            missing = []
            if not self._store_hash:
                missing.append("store_hash")
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
                client.get_store, self._store_hash, self._access_token
            )
            await client.aclose()
            store_name = data.get("name", self._store_hash)
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or self._store_hash,
                message=f"Connected to BigCommerce store: {store_name}",
            )
        except BigCommerceAuthError as exc:
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
        """Ping GET /v2/store and return current health."""
        if not self._store_hash or not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="store_hash and access_token are required",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_store, self._store_hash, self._access_token
            )
            await client.aclose()
            store_name = data.get("name", self._store_hash)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to BigCommerce store: {store_name}",
            )
        except BigCommerceAuthError as exc:
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

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync products, orders, and customers from BigCommerce.

        BigCommerce v2/v3 use page-number pagination (not cursors).
        full=True / since ignored for BigCommerce (no date filter on list endpoints
        without additional query params); all records are always fetched.
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync products (v3 — page/limit with meta.pagination)
        try:
            page = 1
            while True:
                products, total_pages = await with_retry(
                    self.http_client.list_products,
                    self._store_hash,
                    self._access_token,
                    SYNC_PAGE_SIZE,
                    page,
                )
                found += len(products)
                for product in products:
                    try:
                        doc = normalize_product(
                            product, self.connector_id, self._tenant_id, self._store_hash
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if page >= total_pages or not products:
                    break
                page += 1
        except BigCommerceError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Products sync failed: {exc}",
            )

        # Sync orders (v2 — page/limit, returns array directly)
        try:
            page = 1
            while True:
                orders = await with_retry(
                    self.http_client.list_orders,
                    self._store_hash,
                    self._access_token,
                    SYNC_PAGE_SIZE,
                    page,
                )
                found += len(orders)
                for order in orders:
                    try:
                        doc = normalize_order(
                            order, self.connector_id, self._tenant_id, self._store_hash
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                # v2: if fewer records than limit returned, we've hit the last page
                if len(orders) < SYNC_PAGE_SIZE:
                    break
                page += 1
        except BigCommerceError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Orders sync failed: {exc}",
            )

        # Sync customers (v3 — page/limit with meta.pagination)
        try:
            page = 1
            while True:
                customers, total_pages = await with_retry(
                    self.http_client.list_customers,
                    self._store_hash,
                    self._access_token,
                    SYNC_PAGE_SIZE,
                    page,
                )
                found += len(customers)
                for customer in customers:
                    try:
                        doc = normalize_customer(
                            customer, self.connector_id, self._tenant_id, self._store_hash
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if page >= total_pages or not customers:
                    break
                page += 1
        except BigCommerceError as exc:
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

    # ── Products ──────────────────────────────────────────────────────────────

    async def list_products(
        self,
        limit: int = 250,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Return products for a single page."""
        client = self._ensure_client()
        products, _ = await with_retry(
            client.list_products,
            self._store_hash,
            self._access_token,
            limit,
            page,
        )
        return products

    async def get_product(self, product_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(
            client.get_product, self._store_hash, self._access_token, product_id
        )

    # ── Orders ────────────────────────────────────────────────────────────────

    async def list_orders(
        self,
        limit: int = 250,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Return orders for a single page."""
        client = self._ensure_client()
        return await with_retry(
            client.list_orders,
            self._store_hash,
            self._access_token,
            limit,
            page,
        )

    async def get_order(self, order_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(
            client.get_order, self._store_hash, self._access_token, order_id
        )

    # ── Customers ─────────────────────────────────────────────────────────────

    async def list_customers(
        self,
        limit: int = 250,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Return customers for a single page."""
        client = self._ensure_client()
        customers, _ = await with_retry(
            client.list_customers,
            self._store_hash,
            self._access_token,
            limit,
            page,
        )
        return customers

    async def get_customer(self, customer_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(
            client.get_customer, self._store_hash, self._access_token, customer_id
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> BigCommerceConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
