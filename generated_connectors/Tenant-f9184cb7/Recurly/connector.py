from __future__ import annotations

from typing import Any

from client import RecurlyHTTPClient
from exceptions import RecurlyAuthError, RecurlyError, RecurlyNetworkError
from helpers import (
    normalize_account,
    normalize_invoice,
    normalize_plan,
    normalize_subscription,
    normalize_transaction,
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

from shared.base_connector import BaseConnector

CONNECTOR_TYPE: str = "recurly"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 200


class RecurlyConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Recurly subscription billing.

    Syncs accounts, subscriptions, invoices, plans, and transactions from
    Recurly using API Key (HTTP Basic Auth) authentication — API key as
    username, empty string as password.

    Recurly API v3 uses cursor-based pagination: each list response includes
    ``has_more`` and ``next`` (cursor string) fields.
    """

    CONNECTOR_TYPE: str = "recurly"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._api_key: str = _config.get("api_key", "").strip()
        self._subdomain: str = _config.get("subdomain", "").strip()
        self._http_client: RecurlyHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> RecurlyHTTPClient:
        return RecurlyHTTPClient(config=self.config)

    def _ensure_client(self) -> RecurlyHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not self._api_key

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key by calling GET /sites."""
        if self._missing_creds():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: api_key",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_sites)
            await client.aclose()
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Recurly API successfully",
            )
        except RecurlyAuthError as exc:
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
        """Ping GET /sites and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_sites)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Recurly API is reachable",
            )
        except RecurlyAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except RecurlyNetworkError as exc:
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
        **kwargs: Any,
    ) -> SyncResult:
        """
        Sync accounts, subscriptions, invoices, plans, and transactions from Recurly.

        Recurly v3 uses cursor-based pagination: ``has_more`` + ``next`` (cursor)
        in every list response. Iteration stops when ``has_more`` is False.
        """
        if self._http_client is None:
            self._http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # ── Accounts ──────────────────────────────────────────────────────────
        try:
            cursor: str | None = None
            while True:
                response = await with_retry(
                    self._http_client.get_accounts,
                    limit=SYNC_PAGE_SIZE,
                    cursor=cursor,
                )
                items: list[dict[str, Any]] = response.get("data", [])
                if not items:
                    break
                found += len(items)
                for item in items:
                    try:
                        doc = normalize_account(item, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if not response.get("has_more", False):
                    break
                cursor = response.get("next") or None
                if not cursor:
                    break
        except RecurlyError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Accounts sync failed: {exc}",
            )

        # ── Subscriptions ──────────────────────────────────────────────────────
        try:
            cursor = None
            while True:
                response = await with_retry(
                    self._http_client.get_subscriptions,
                    limit=SYNC_PAGE_SIZE,
                    cursor=cursor,
                )
                items = response.get("data", [])
                if not items:
                    break
                found += len(items)
                for item in items:
                    try:
                        doc = normalize_subscription(item, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if not response.get("has_more", False):
                    break
                cursor = response.get("next") or None
                if not cursor:
                    break
        except RecurlyError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Subscriptions sync failed: {exc}",
            )

        # ── Invoices ──────────────────────────────────────────────────────────
        try:
            cursor = None
            while True:
                response = await with_retry(
                    self._http_client.get_invoices,
                    limit=SYNC_PAGE_SIZE,
                    cursor=cursor,
                )
                items = response.get("data", [])
                if not items:
                    break
                found += len(items)
                for item in items:
                    try:
                        doc = normalize_invoice(item, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if not response.get("has_more", False):
                    break
                cursor = response.get("next") or None
                if not cursor:
                    break
        except RecurlyError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Invoices sync failed: {exc}",
            )

        # ── Plans ─────────────────────────────────────────────────────────────
        try:
            response = await with_retry(
                self._http_client.get_plans,
                limit=SYNC_PAGE_SIZE,
            )
            items = response.get("data", [])
            found += len(items)
            for item in items:
                try:
                    doc = normalize_plan(item, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except RecurlyError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Plans sync failed: {exc}",
            )

        # ── Transactions ──────────────────────────────────────────────────────
        try:
            cursor = None
            while True:
                response = await with_retry(
                    self._http_client.get_transactions,
                    limit=SYNC_PAGE_SIZE,
                    cursor=cursor,
                )
                items = response.get("data", [])
                if not items:
                    break
                found += len(items)
                for item in items:
                    try:
                        doc = normalize_transaction(item, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if not response.get("has_more", False):
                    break
                cursor = response.get("next") or None
                if not cursor:
                    break
        except RecurlyError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Transactions sync failed: {exc}",
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

    # ── Resource list methods ─────────────────────────────────────────────────

    async def list_accounts(self) -> list[dict[str, Any]]:
        """Return all accounts, paginating through all cursor pages."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = await with_retry(client.get_accounts, limit=SYNC_PAGE_SIZE, cursor=cursor)
            items: list[dict[str, Any]] = response.get("data", [])
            results.extend(items)
            if not response.get("has_more", False) or not items:
                break
            cursor = response.get("next") or None
            if not cursor:
                break
        return results

    async def list_subscriptions(self) -> list[dict[str, Any]]:
        """Return all subscriptions, paginating through all cursor pages."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = await with_retry(
                client.get_subscriptions, limit=SYNC_PAGE_SIZE, cursor=cursor
            )
            items = response.get("data", [])
            results.extend(items)
            if not response.get("has_more", False) or not items:
                break
            cursor = response.get("next") or None
            if not cursor:
                break
        return results

    async def list_invoices(self) -> list[dict[str, Any]]:
        """Return all invoices, paginating through all cursor pages."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = await with_retry(
                client.get_invoices, limit=SYNC_PAGE_SIZE, cursor=cursor
            )
            items = response.get("data", [])
            results.extend(items)
            if not response.get("has_more", False) or not items:
                break
            cursor = response.get("next") or None
            if not cursor:
                break
        return results

    async def list_plans(self) -> list[dict[str, Any]]:
        """Return all plans."""
        client = self._ensure_client()
        response = await with_retry(client.get_plans, limit=SYNC_PAGE_SIZE)
        return response.get("data", [])

    async def list_transactions(self) -> list[dict[str, Any]]:
        """Return all transactions, paginating through all cursor pages."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = await with_retry(
                client.get_transactions, limit=SYNC_PAGE_SIZE, cursor=cursor
            )
            items = response.get("data", [])
            results.extend(items)
            if not response.get("has_more", False) or not items:
                break
            cursor = response.get("next") or None
            if not cursor:
                break
        return results

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> RecurlyConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
