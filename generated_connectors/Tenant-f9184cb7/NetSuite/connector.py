from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from client.http_client import NetSuiteHTTPClient
from exceptions import (
    NetSuiteAuthError,
    NetSuiteError,
    NetSuiteNetworkError,
    NetSuiteValidationError,
)
from helpers.utils import (
    normalize_customer,
    normalize_invoice,
    normalize_item,
    with_retry,
)
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

CONNECTOR_TYPE = "netsuite"
SYNC_MAX_RESULTS = 100

# Required install fields
_REQUIRED_FIELDS = (
    "account_id",
    "consumer_key",
    "consumer_secret",
    "token_key",
    "token_secret",
)


class NetSuiteConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Oracle NetSuite.

    Provides Token-Based Authentication (OAuth 1.0a with HMAC-SHA256),
    health checks, full sync (customers, invoices, items), direct REST API
    access to customer/invoice/item records, and SuiteQL query support.
    """

    CONNECTOR_TYPE: str = "netsuite"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(  # type: ignore[call-arg]
                tenant_id=tenant_id,
                connector_id=connector_id,
                config=_config,
            )
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        # TBA install fields
        self._account_id: str = _config.get("account_id", "")
        self._consumer_key: str = _config.get("consumer_key", "")
        self._consumer_secret: str = _config.get("consumer_secret", "")
        self._token_key: str = _config.get("token_key", "")
        self._token_secret: str = _config.get("token_secret", "")

        self.http_client: NetSuiteHTTPClient | None = None

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _missing_fields(self) -> List[str]:
        """Return a list of required config fields that are absent or empty."""
        return [f for f in _REQUIRED_FIELDS if not self.config.get(f, "")]

    def _make_client(self) -> NetSuiteHTTPClient:
        return NetSuiteHTTPClient(
            account_id=self._account_id,
            consumer_key=self._consumer_key,
            consumer_secret=self._consumer_secret,
            token_key=self._token_key,
            token_secret=self._token_secret,
        )

    def _ensure_client(self) -> NetSuiteHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """
        Validate all five TBA install fields and verify connectivity.

        Hits GET /record/v1/customer?limit=1 to confirm the credentials
        produce a valid OAuth 1.0a signature that NetSuite accepts.
        """
        missing = self._missing_fields()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            await with_retry(client.list_customers, 1, 0)
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or self._account_id,
                message=(
                    f"Connected to NetSuite account {self._account_id} via "
                    "Token-Based Authentication."
                ),
            )
        except NetSuiteAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid TBA credentials: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Verify connectivity via GET /record/v1/customer?limit=1."""
        missing = self._missing_fields()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            await with_retry(client.list_customers, 1, 0)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=(
                    f"NetSuite REST API reachable — account {self._account_id}"
                ),
            )
        except NetSuiteAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except NetSuiteNetworkError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
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
        full: bool = True,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync NetSuite customers, invoices, and items into the knowledge base.

        Fetches up to SYNC_MAX_RESULTS of each entity type.
        """
        missing = self._missing_fields()
        if missing:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0

        # Customers
        try:
            cust_resp = await with_retry(
                client.list_customers, SYNC_MAX_RESULTS, 0
            )
            customers: List[dict[str, Any]] = cust_resp.get("items", [])
            found += len(customers)
            for cust in customers:
                try:
                    doc = normalize_customer(
                        cust, self.connector_id, self._tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except NetSuiteError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Customer sync failed: {exc}",
            )

        # Invoices
        try:
            inv_resp = await with_retry(
                client.list_invoices, SYNC_MAX_RESULTS, 0
            )
            invoices: List[dict[str, Any]] = inv_resp.get("items", [])
            found += len(invoices)
            for inv in invoices:
                try:
                    doc = normalize_invoice(
                        inv, self.connector_id, self._tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except NetSuiteError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Invoice sync failed: {exc}",
            )

        # Items
        try:
            item_resp = await with_retry(
                client.list_items, SYNC_MAX_RESULTS, 0
            )
            items: List[dict[str, Any]] = item_resp.get("items", [])
            found += len(items)
            for item in items:
                try:
                    doc = normalize_item(
                        item, self.connector_id, self._tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except NetSuiteError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Item sync failed: {exc}",
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

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """GET /record/v1/customer — paginated customer list."""
        client = self._ensure_client()
        return await with_retry(client.list_customers, limit, offset)

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        """GET /record/v1/customer/{customer_id} — single customer by internal ID."""
        client = self._ensure_client()
        return await with_retry(client.get_customer, customer_id)

    # ── Invoices ─────────────────────────────────────────────────────────────

    async def list_invoices(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """GET /record/v1/invoice — paginated invoice list."""
        client = self._ensure_client()
        return await with_retry(client.list_invoices, limit, offset)

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        """GET /record/v1/invoice/{invoice_id} — single invoice by internal ID."""
        client = self._ensure_client()
        return await with_retry(client.get_invoice, invoice_id)

    # ── Items ─────────────────────────────────────────────────────────────────

    async def list_items(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """GET /record/v1/item — paginated item list."""
        client = self._ensure_client()
        return await with_retry(client.list_items, limit, offset)

    # ── SuiteQL ──────────────────────────────────────────────────────────────

    async def suiteql(
        self, query: str, limit: int = 1000, offset: int = 0
    ) -> dict[str, Any]:
        """POST /query/v1/suiteql — execute a SuiteQL query.

        SuiteQL is NetSuite's SQL dialect. Example:
          SELECT id, companyName, email FROM customer WHERE isInactive = 'F'
        """
        client = self._ensure_client()
        return await with_retry(client.suiteql, query, limit, offset)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> NetSuiteConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
