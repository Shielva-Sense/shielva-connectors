from __future__ import annotations

from typing import Any, Dict

from client import BrevoHTTPClient
from exceptions import (
    BrevoAuthError,
    BrevoError,
    BrevoNetworkError,
)
from helpers import (
    normalize_campaign,
    normalize_contact,
    normalize_template,
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
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(self, tenant_id: str = "", connector_id: str = "", config: Dict[str, Any] | None = None) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

SYNC_PAGE_SIZE = 50


class BrevoConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Brevo (formerly Sendinblue).

    Provides authentication, health checks, full sync, and direct access to
    Brevo contacts, email campaigns, contact lists, senders, and SMTP templates
    via the Brevo REST API v3.

    Authentication uses an API key passed in the ``api-key`` header
    (not Bearer, not Authorization — this is Brevo-specific).
    """

    CONNECTOR_TYPE: str = "brevo"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._api_key: str = _config.get("api_key", "")
        self.http_client: BrevoHTTPClient | None = None

    def _make_client(self) -> BrevoHTTPClient:
        return BrevoHTTPClient(api_key=self._api_key)

    def _has_credentials(self) -> bool:
        return bool(self._api_key)

    def _ensure_client(self) -> BrevoHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the api_key by calling GET /v3/account."""
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
                message="Connected to Brevo",
            )
        except BrevoAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Brevo authentication failed: {exc}",
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
        """Ping GET /v3/account and return current health with email/plan."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_account)
            await client.aclose()
            email = data.get("email", "")
            first = data.get("firstName", "") or ""
            last = data.get("lastName", "") or ""
            user_name = f"{first} {last}".strip()
            plan_info = data.get("plan", [])
            plan = plan_info[0].get("type", "") if plan_info else ""
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Brevo API is reachable. Plan: {plan}" if plan else "Brevo API is reachable",
                user_name=user_name,
                user_email=email,
            )
        except BrevoAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except BrevoNetworkError as exc:
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
        """
        Sync Brevo contacts and email campaigns into the knowledge base.

        Uses offset-based pagination (limit + offset). Response has ``count`` field.
        """
        kb_id: str = kwargs.get("kb_id", "")
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Contacts
        try:
            contacts = await self._fetch_all_contacts()
        except BrevoError as exc:
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

        # Email campaigns
        try:
            campaigns = await self._fetch_all_campaigns()
        except BrevoError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL if synced > 0 else SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )
        found += len(campaigns)
        for record in campaigns:
            try:
                doc = normalize_campaign(record, self.connector_id, self.tenant_id)
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
        offset = 0
        while True:
            page = await with_retry(
                self.http_client.get_contacts, limit=SYNC_PAGE_SIZE, offset=offset
            )
            batch: list[dict[str, Any]] = page.get("contacts", []) or []
            records.extend(batch)
            total = int(page.get("count", 0))
            offset += len(batch)
            if not batch or offset >= total:
                break
        return records

    async def _fetch_all_campaigns(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await with_retry(
                self.http_client.get_email_campaigns, limit=SYNC_PAGE_SIZE, offset=offset
            )
            batch: list[dict[str, Any]] = page.get("campaigns", []) or []
            records.extend(batch)
            total = int(page.get("count", 0))
            offset += len(batch)
            if not batch or offset >= total:
                break
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(
        self, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """Paginated list of Brevo contacts."""
        client = self._ensure_client()
        return await with_retry(client.get_contacts, limit=limit, offset=offset)

    async def get_contact(self, identifier: str) -> dict[str, Any]:
        """Retrieve a single contact by email or id."""
        client = self._ensure_client()
        return await with_retry(client.get_contact, identifier)

    async def list_contact_lists(
        self, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """Paginated list of Brevo contact lists."""
        client = self._ensure_client()
        return await with_retry(client.get_contact_lists, limit=limit, offset=offset)

    # ── Campaigns ─────────────────────────────────────────────────────────────

    async def list_campaigns(
        self, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        """Paginated list of Brevo email campaigns, optionally filtered by status."""
        client = self._ensure_client()
        return await with_retry(
            client.get_email_campaigns, limit=limit, offset=offset, status=status
        )

    # ── Senders ───────────────────────────────────────────────────────────────

    async def list_senders(self) -> dict[str, Any]:
        """List all Brevo senders."""
        client = self._ensure_client()
        return await with_retry(client.get_senders)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> BrevoConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
