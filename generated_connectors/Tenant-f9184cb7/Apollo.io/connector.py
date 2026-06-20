from __future__ import annotations

from typing import Any

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config: dict[str, Any] = config or {}

from client import ApolloHTTPClient
from exceptions import ApolloAuthError, ApolloError, ApolloNetworkError
from helpers import normalize_account, normalize_contact, normalize_person, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

SYNC_PAGE_SIZE = 50


class ApolloConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Apollo.io sales intelligence platform.

    Provides authentication, health checks, full sync, and direct access to
    Apollo.io people, contacts, accounts, and sequences via the REST API v1.
    """

    CONNECTOR_TYPE: str = "apollo"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._api_key: str = _config.get("api_key", "")
        self.http_client: ApolloHTTPClient | None = None

    def _make_client(self) -> ApolloHTTPClient:
        return ApolloHTTPClient(api_key=self._api_key)

    def _ensure_client(self) -> ApolloHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    def _has_credentials(self) -> bool:
        return bool(self._api_key)

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the API key by calling POST /v1/auth/health."""
        if not self._has_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_account)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Apollo.io",
            )
        except ApolloAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Apollo.io authentication failed: {exc}",
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
        """Ping POST /v1/auth/health and return current health status."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_account)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Apollo.io API is reachable",
            )
        except ApolloAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ApolloNetworkError as exc:
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

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync Apollo.io contacts and accounts into the knowledge base.

        Fetches all pages of contacts and accounts, normalizes each record into
        a ConnectorDocument, and optionally ingests into the knowledge base.
        """
        kb_id: str = kwargs.get("kb_id", "")
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync contacts
        try:
            contacts = await self._fetch_all_contacts()
        except ApolloError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        found += len(contacts)
        for record in contacts:
            try:
                doc = normalize_contact(record, self.connector_id, self.tenant_id)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # Sync accounts
        try:
            accounts = await self._fetch_all_accounts()
        except ApolloError as exc:
            status = SyncStatus.PARTIAL if (synced > 0 or failed > 0) else SyncStatus.FAILED
            return SyncResult(
                status=status,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        found += len(accounts)
        for record in accounts:
            try:
                doc = normalize_account(record, self.connector_id, self.tenant_id)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_contacts(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await with_retry(
                self.http_client.search_contacts, page=page, per_page=SYNC_PAGE_SIZE
            )
            batch: list[dict[str, Any]] = resp.get("contacts", [])
            records.extend(batch)
            pagination = resp.get("pagination", {})
            total_pages: int = pagination.get("total_pages", 1)
            if page >= total_pages or not batch:
                break
            page += 1
        return records

    async def _fetch_all_accounts(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await with_retry(
                self.http_client.search_accounts, page=page, per_page=SYNC_PAGE_SIZE
            )
            batch: list[dict[str, Any]] = resp.get("accounts", [])
            records.extend(batch)
            pagination = resp.get("pagination", {})
            total_pages: int = pagination.get("total_pages", 1)
            if page >= total_pages or not batch:
                break
            page += 1
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── People ────────────────────────────────────────────────────────────────

    async def list_people(self, page: int = 1) -> dict[str, Any]:
        """POST /v1/mixed_people/search — list people from the Apollo database."""
        client = self._ensure_client()
        return await with_retry(client.search_people, page=page, per_page=SYNC_PAGE_SIZE)

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(self, page: int = 1) -> dict[str, Any]:
        """POST /v1/contacts/search — list CRM contacts."""
        client = self._ensure_client()
        return await with_retry(client.search_contacts, page=page, per_page=SYNC_PAGE_SIZE)

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        """GET /v1/contacts/{contact_id} — retrieve a single contact by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_contact, contact_id)

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def list_accounts(self, page: int = 1) -> dict[str, Any]:
        """POST /v1/accounts/search — list CRM accounts."""
        client = self._ensure_client()
        return await with_retry(client.search_accounts, page=page, per_page=SYNC_PAGE_SIZE)

    async def get_account(self, account_id: str) -> dict[str, Any]:
        """GET /v1/accounts/{account_id} — retrieve a single account by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_account_details, account_id)

    # ── Sequences ─────────────────────────────────────────────────────────────

    async def list_sequences(self) -> dict[str, Any]:
        """GET /v1/emailer_campaigns — list email sequences."""
        client = self._ensure_client()
        return await with_retry(client.list_sequences, page=1)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> ApolloConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
