from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import WooCommerceHTTPClient
from exceptions import WooCommerceAuthError, WooCommerceError, WooCommerceNetworkError
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
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(self, tenant_id: str = "", connector_id: str = "", config: dict | None = None) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

SYNC_PAGE_SIZE = 100


class WooCommerceConnector(BaseConnector):
    """
    Shielva connector for WooCommerce.

    Syncs products, orders, and customers from a WooCommerce store via
    the WooCommerce REST API v3, using Consumer Key / Consumer Secret
    HTTP Basic authentication.
    """

    CONNECTOR_TYPE: str = "woocommerce"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
        # Convenience kwargs for standalone / test usage
        site_url: str = "",
        store_url: str = "",  # backwards-compat alias
        consumer_key: str = "",
        consumer_secret: str = "",
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)

        # Support both "site_url" (spec) and "store_url" (legacy alias)
        self._site_url: str = (
            _config.get("site_url", "")
            or _config.get("store_url", "")
            or site_url
            or store_url
        )
        # Keep _store_url as alias so existing test code that sets _http still works
        self._store_url: str = self._site_url
        self._consumer_key: str = _config.get("consumer_key", "") or consumer_key
        self._consumer_secret: str = _config.get("consumer_secret", "") or consumer_secret
        self._http: WooCommerceHTTPClient = WooCommerceHTTPClient()

    # ── Credentials helpers ───────────────────────────────────────────────────

    def _has_credentials(self) -> bool:
        return bool(self._site_url and self._consumer_key and self._consumer_secret)

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling /system_status."""
        if not self._has_credentials():
            missing: list[str] = []
            if not self._site_url:
                missing.append("site_url")
            if not self._consumer_key:
                missing.append("consumer_key")
            if not self._consumer_secret:
                missing.append("consumer_secret")
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        try:
            data = await with_retry(
                self._http.get_system_status,
                self._site_url,
                self._consumer_key,
                self._consumer_secret,
            )
            wc_version: str = (
                data.get("environment", {}).get("wp_version", "")
                or data.get("wp_version", "unknown")
            )
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to WooCommerce store (WP version {wc_version})",
            )
        except WooCommerceAuthError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping /system_status and return current connector health."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing credentials",
            )

        try:
            await with_retry(
                self._http.get_system_status,
                self._site_url,
                self._consumer_key,
                self._consumer_secret,
            )
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="WooCommerce store is reachable",
            )
        except WooCommerceAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except WooCommerceNetworkError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
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
        Sync orders, products, and customers into the knowledge base.

        full=True  → fetch all records regardless of modification time.
        since=<dt> → fetch only records modified after that timestamp (ISO 8601).
        """
        modified_after: str | None = None
        if not full and since is not None:
            modified_after = since.isoformat()

        found = 0
        synced = 0
        failed = 0
        overall_error: str = ""

        # Sync orders
        try:
            o_found, o_synced, o_failed = await self._sync_resource(
                "orders", modified_after=modified_after, kb_id=kb_id
            )
            found += o_found
            synced += o_synced
            failed += o_failed
        except WooCommerceError as exc:
            overall_error = str(exc)

        # Sync products
        try:
            p_found, p_synced, p_failed = await self._sync_resource(
                "products", modified_after=modified_after, kb_id=kb_id
            )
            found += p_found
            synced += p_synced
            failed += p_failed
        except WooCommerceError as exc:
            overall_error = overall_error or str(exc)

        # Sync customers
        try:
            c_found, c_synced, c_failed = await self._sync_resource(
                "customers", modified_after=modified_after, kb_id=kb_id
            )
            found += c_found
            synced += c_synced
            failed += c_failed
        except WooCommerceError as exc:
            overall_error = overall_error or str(exc)

        if found == 0 and overall_error:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=overall_error,
            )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
            message=overall_error,
        )

    async def _sync_resource(
        self,
        resource: str,
        modified_after: str | None,
        kb_id: str,
    ) -> tuple[int, int, int]:
        """Paginate through a resource and ingest each item. Returns (found, synced, failed)."""
        found = 0
        synced = 0
        failed = 0
        page = 1

        while True:
            if resource == "orders":
                items, headers = await with_retry(
                    self._http.list_orders,
                    self._site_url,
                    self._consumer_key,
                    self._consumer_secret,
                    page=page,
                    per_page=SYNC_PAGE_SIZE,
                    modified_after=modified_after,
                )
            elif resource == "products":
                items, headers = await with_retry(
                    self._http.list_products,
                    self._site_url,
                    self._consumer_key,
                    self._consumer_secret,
                    page=page,
                    per_page=SYNC_PAGE_SIZE,
                    modified_after=modified_after,
                )
            else:  # customers
                items, headers = await with_retry(
                    self._http.list_customers,
                    self._site_url,
                    self._consumer_key,
                    self._consumer_secret,
                    page=page,
                    per_page=SYNC_PAGE_SIZE,
                    modified_after=modified_after,
                )

            if not items:
                break

            found += len(items)

            for raw in items:
                try:
                    doc = self._normalize(resource, raw)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # Check X-WP-TotalPages to decide whether to continue
            total_pages_raw = headers.get("X-WP-TotalPages") or headers.get("x-wp-totalpages")
            if total_pages_raw is not None:
                # WooCommerce told us the total pages — use it
                total_pages = int(total_pages_raw)
                if page >= total_pages:
                    break
            else:
                # No header: stop when we got fewer items than requested
                if len(items) < SYNC_PAGE_SIZE:
                    break
            page += 1

        return found, synced, failed

    def _normalize(self, resource: str, raw: dict[str, Any]) -> ConnectorDocument:
        if resource == "orders":
            return normalize_order(raw, self.connector_id, self._tenant_id, self._site_url)
        if resource == "products":
            return normalize_product(raw, self.connector_id, self._tenant_id, self._site_url)
        # customers
        return normalize_customer(raw, self.connector_id, self._tenant_id, self._site_url)

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Direct API methods ────────────────────────────────────────────────────

    async def list_orders(
        self,
        page: int = 1,
        per_page: int = SYNC_PAGE_SIZE,
        status: str = "any",
        modified_after: str | None = None,
    ) -> list[dict[str, Any]]:
        items, _ = await with_retry(
            self._http.list_orders,
            self._site_url,
            self._consumer_key,
            self._consumer_secret,
            page=page,
            per_page=per_page,
            status=status,
            modified_after=modified_after,
        )
        return items

    async def get_order(self, order_id: int) -> dict[str, Any]:
        return await with_retry(
            self._http.get_order,
            self._site_url,
            self._consumer_key,
            self._consumer_secret,
            order_id,
        )

    async def list_products(
        self,
        page: int = 1,
        per_page: int = SYNC_PAGE_SIZE,
        status: str = "any",
        modified_after: str | None = None,
    ) -> list[dict[str, Any]]:
        items, _ = await with_retry(
            self._http.list_products,
            self._site_url,
            self._consumer_key,
            self._consumer_secret,
            page=page,
            per_page=per_page,
            status=status,
            modified_after=modified_after,
        )
        return items

    async def get_product(self, product_id: int) -> dict[str, Any]:
        return await with_retry(
            self._http.get_product,
            self._site_url,
            self._consumer_key,
            self._consumer_secret,
            product_id,
        )

    async def list_customers(
        self,
        page: int = 1,
        per_page: int = SYNC_PAGE_SIZE,
        modified_after: str | None = None,
    ) -> list[dict[str, Any]]:
        items, _ = await with_retry(
            self._http.list_customers,
            self._site_url,
            self._consumer_key,
            self._consumer_secret,
            page=page,
            per_page=per_page,
            modified_after=modified_after,
        )
        return items

    async def get_customer(self, customer_id: int) -> dict[str, Any]:
        return await with_retry(
            self._http.get_customer,
            self._site_url,
            self._consumer_key,
            self._consumer_secret,
            customer_id,
        )

    async def list_categories(
        self,
        page: int = 1,
        per_page: int = SYNC_PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        """Return product categories for a single page."""
        items, _ = await with_retry(
            self._http.list_categories,
            self._site_url,
            self._consumer_key,
            self._consumer_secret,
            page=page,
            per_page=per_page,
        )
        return items

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        pass  # aiohttp sessions are per-request; no persistent connection to close

    async def __aenter__(self) -> WooCommerceConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
