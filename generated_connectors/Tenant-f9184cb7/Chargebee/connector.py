from __future__ import annotations

from typing import Any, Dict

from client import ChargebeeHTTPClient
from exceptions import ChargebeeAuthError, ChargebeeError, ChargebeeNetworkError
from helpers import normalize_customer, normalize_invoice, normalize_subscription, with_retry
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

CONNECTOR_TYPE: str = "chargebee"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 100


class ChargebeeConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Chargebee.

    Syncs subscriptions, customers, and invoices from a Chargebee site
    using API Key (HTTP Basic) authentication — API key as username, empty
    string as password.
    """

    CONNECTOR_TYPE: str = "chargebee"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(
                tenant_id=tenant_id, connector_id=connector_id, config=_config
            )
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        self._site: str = _config.get("site", "").strip().rstrip("/")
        self._api_key: str = _config.get("api_key", "").strip()
        self._http_client: ChargebeeHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> ChargebeeHTTPClient:
        return ChargebeeHTTPClient()

    def _ensure_client(self) -> ChargebeeHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not self._site or not self._api_key

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate site + api_key by calling GET /subscriptions?limit=1."""
        if self._missing_creds():
            missing = []
            if not self._site:
                missing.append("site")
            if not self._api_key:
                missing.append("api_key")
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            response = await with_retry(
                client.list_subscriptions,
                self._site,
                self._api_key,
                limit=1,
            )
            await client.aclose()
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Chargebee site '{self._site}'",
            )
        except ChargebeeAuthError as exc:
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

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /subscriptions?limit=1 and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="site and api_key are required",
            )

        client = self._make_client()
        try:
            await with_retry(
                client.list_subscriptions,
                self._site,
                self._api_key,
                limit=1,
            )
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Chargebee API is reachable",
            )
        except ChargebeeAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ChargebeeNetworkError as exc:
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

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync subscriptions, customers, and invoices from Chargebee.

        Chargebee uses offset-based pagination: each response contains a
        ``next_offset`` field that must be passed as ``offset`` in the next
        request. Iteration stops when ``next_offset`` is absent or empty.
        """
        if self._http_client is None:
            self._http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync subscriptions
        try:
            offset: str | None = None
            while True:
                response = await with_retry(
                    self._http_client.list_subscriptions,
                    self._site,
                    self._api_key,
                    limit=SYNC_PAGE_SIZE,
                    offset=offset,
                )
                items: list[dict[str, Any]] = response.get("list", [])
                if not items:
                    break
                found += len(items)

                for item in items:
                    try:
                        doc = normalize_subscription(
                            item,
                            self.connector_id,
                            self._tenant_id,
                            self._site,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                offset = response.get("next_offset") or None
                if not offset:
                    break
        except ChargebeeError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # Sync customers
        try:
            offset = None
            while True:
                response = await with_retry(
                    self._http_client.list_customers,
                    self._site,
                    self._api_key,
                    limit=SYNC_PAGE_SIZE,
                    offset=offset,
                )
                items = response.get("list", [])
                if not items:
                    break
                found += len(items)

                for item in items:
                    try:
                        doc = normalize_customer(
                            item,
                            self.connector_id,
                            self._tenant_id,
                            self._site,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                offset = response.get("next_offset") or None
                if not offset:
                    break
        except ChargebeeError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Customers sync failed: {exc}",
            )

        # Sync invoices
        try:
            offset = None
            while True:
                response = await with_retry(
                    self._http_client.list_invoices,
                    self._site,
                    self._api_key,
                    limit=SYNC_PAGE_SIZE,
                    offset=offset,
                )
                items = response.get("list", [])
                if not items:
                    break
                found += len(items)

                for item in items:
                    try:
                        doc = normalize_invoice(
                            item,
                            self.connector_id,
                            self._tenant_id,
                            self._site,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                offset = response.get("next_offset") or None
                if not offset:
                    break
        except ChargebeeError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Invoices sync failed: {exc}",
            )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Subscription methods ──────────────────────────────────────────────────

    async def list_subscriptions(
        self,
        limit: int = 100,
        offset: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of subscriptions; empty ``list`` key signals end of pagination."""
        client = self._ensure_client()
        return await with_retry(
            client.list_subscriptions,
            self._site,
            self._api_key,
            limit=limit,
            offset=offset,
        )

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Return a single subscription by ID (unwraps the Chargebee envelope)."""
        client = self._ensure_client()
        response = await with_retry(
            client.get_subscription, self._site, self._api_key, subscription_id
        )
        return response.get("subscription", response)

    # ── Customer methods ──────────────────────────────────────────────────────

    async def list_customers(
        self,
        limit: int = 100,
        offset: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of customers; empty ``list`` key signals end of pagination."""
        client = self._ensure_client()
        return await with_retry(
            client.list_customers,
            self._site,
            self._api_key,
            limit=limit,
            offset=offset,
        )

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        """Return a single customer by ID (unwraps the Chargebee envelope)."""
        client = self._ensure_client()
        response = await with_retry(
            client.get_customer, self._site, self._api_key, customer_id
        )
        return response.get("customer", response)

    # ── Invoice methods ───────────────────────────────────────────────────────

    async def list_invoices(
        self,
        limit: int = 100,
        offset: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of invoices; empty ``list`` key signals end of pagination."""
        client = self._ensure_client()
        return await with_retry(
            client.list_invoices,
            self._site,
            self._api_key,
            limit=limit,
            offset=offset,
        )

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        """Return a single invoice by ID (unwraps the Chargebee envelope)."""
        client = self._ensure_client()
        response = await with_retry(
            client.get_invoice, self._site, self._api_key, invoice_id
        )
        return response.get("invoice", response)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> ChargebeeConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
