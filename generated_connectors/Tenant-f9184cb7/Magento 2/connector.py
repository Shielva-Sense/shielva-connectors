from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import MagentoHTTPClient
from exceptions import MagentoAuthError, MagentoError
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


class MagentoConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Magento 2.

    Provides authentication, health checks, full/incremental sync, and
    direct access to Magento orders, products, customers, and categories
    via the Magento 2 REST API (V1).
    """

    CONNECTOR_TYPE: str = "magento"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
        # Standalone / test convenience args
        base_url: str = "",
        access_token: str = "",
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        self._base_url: str = _config.get("base_url", "") or base_url
        self._access_token: str = _config.get("access_token", "") or access_token
        self.http_client: MagentoHTTPClient | None = None

    def _make_client(self) -> MagentoHTTPClient:
        return MagentoHTTPClient()

    def _ensure_client(self) -> MagentoHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling GET /store/storeConfigs."""
        if not self._base_url or not self._access_token:
            missing = []
            if not self._base_url:
                missing.append("base_url")
            if not self._access_token:
                missing.append("access_token")
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            configs = await with_retry(
                client.get_store_info, self._base_url, self._access_token
            )
            await client.aclose()
            store_name = ""
            if configs and isinstance(configs, list):
                first = configs[0]
                store_name = first.get("base_url", self._base_url) or self._base_url
                store_name = first.get("store_name", store_name) or store_name
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Magento store: {store_name or self._base_url}",
            )
        except MagentoAuthError as exc:
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
        """Ping GET /store/storeConfigs and return current health."""
        if not self._base_url or not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="base_url and access_token are required",
            )

        client = self._make_client()
        try:
            configs = await with_retry(
                client.get_store_info, self._base_url, self._access_token
            )
            await client.aclose()
            store_name = self._base_url
            if configs and isinstance(configs, list):
                first = configs[0]
                store_name = first.get("store_name", store_name) or store_name
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Magento store: {store_name}",
            )
        except MagentoAuthError as exc:
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
        Sync orders, products, and customers from Magento.

        full=True → fetch all records.
        since=<datetime> → fetch records created after that timestamp (ISO 8601).
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        created_after: str | None = None
        if not full and since:
            created_after = since.isoformat()

        found = 0
        synced = 0
        failed = 0

        # Sync orders
        try:
            page = 1
            while True:
                resp = await with_retry(
                    self.http_client.list_orders,
                    self._base_url,
                    self._access_token,
                    page,
                    SYNC_PAGE_SIZE,
                    "created_at",
                    "DESC",
                    created_after,
                )
                items: list[dict[str, Any]] = resp.get("items", [])
                total_count: int = resp.get("total_count", 0)
                found += len(items)
                for order in items:
                    try:
                        doc = normalize_order(order, self.connector_id, self._tenant_id, self._base_url)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if page * SYNC_PAGE_SIZE >= total_count:
                    break
                page += 1
        except MagentoError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Orders sync failed: {exc}",
            )

        # Sync products
        try:
            page = 1
            while True:
                resp = await with_retry(
                    self.http_client.list_products,
                    self._base_url,
                    self._access_token,
                    page,
                    SYNC_PAGE_SIZE,
                    created_after,
                )
                items = resp.get("items", [])
                total_count = resp.get("total_count", 0)
                found += len(items)
                for product in items:
                    try:
                        doc = normalize_product(product, self.connector_id, self._tenant_id, self._base_url)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if page * SYNC_PAGE_SIZE >= total_count:
                    break
                page += 1
        except MagentoError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Products sync failed: {exc}",
            )

        # Sync customers
        try:
            page = 1
            while True:
                resp = await with_retry(
                    self.http_client.list_customers,
                    self._base_url,
                    self._access_token,
                    page,
                    SYNC_PAGE_SIZE,
                    created_after,
                )
                items = resp.get("items", [])
                total_count = resp.get("total_count", 0)
                found += len(items)
                for customer in items:
                    try:
                        doc = normalize_customer(customer, self.connector_id, self._tenant_id, self._base_url)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if page * SYNC_PAGE_SIZE >= total_count:
                    break
                page += 1
        except MagentoError as exc:
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

    # ── Orders ────────────────────────────────────────────────────────────────

    async def list_orders(
        self,
        page: int = 1,
        page_size: int = 100,
        created_after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return orders for the given page (Magento offset pagination)."""
        client = self._ensure_client()
        resp = await with_retry(
            client.list_orders,
            self._base_url,
            self._access_token,
            page,
            page_size,
            "created_at",
            "DESC",
            created_after,
        )
        return resp.get("items", [])

    async def get_order(self, order_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(
            client.get_order, self._base_url, self._access_token, order_id
        )

    # ── Products ──────────────────────────────────────────────────────────────

    async def list_products(
        self,
        page: int = 1,
        page_size: int = 100,
        created_after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return products for the given page."""
        client = self._ensure_client()
        resp = await with_retry(
            client.list_products,
            self._base_url,
            self._access_token,
            page,
            page_size,
            created_after,
        )
        return resp.get("items", [])

    async def get_product(self, sku: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(
            client.get_product, self._base_url, self._access_token, sku
        )

    # ── Customers ─────────────────────────────────────────────────────────────

    async def list_customers(
        self,
        page: int = 1,
        page_size: int = 100,
        created_after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return customers for the given page."""
        client = self._ensure_client()
        resp = await with_retry(
            client.list_customers,
            self._base_url,
            self._access_token,
            page,
            page_size,
            created_after,
        )
        return resp.get("items", [])

    async def get_customer(self, customer_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(
            client.get_customer, self._base_url, self._access_token, customer_id
        )

    # ── Categories ────────────────────────────────────────────────────────────

    async def list_categories(self) -> dict[str, Any]:
        """Return full Magento category tree."""
        client = self._ensure_client()
        return await with_retry(
            client.list_categories, self._base_url, self._access_token
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> MagentoConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
