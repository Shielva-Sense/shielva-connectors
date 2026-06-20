from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict

from client import SendGridHTTPClient
from exceptions import (
    SendGridAuthError,
    SendGridError,
    SendGridNetworkError,
)
from helpers import normalize_contact, normalize_template, with_retry
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

SYNC_PAGE_SIZE: int = 1000


class SendGridConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for SendGrid (email delivery platform by Twilio).

    Provides authentication, health checks, full sync, and direct access to
    SendGrid marketing contacts, templates, lists, segments, stats, and
    suppression groups via the SendGrid Web API v3.

    Authentication: ``Authorization: Bearer {api_key}`` header.
    """

    CONNECTOR_TYPE: str = "sendgrid"
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
        self.http_client: SendGridHTTPClient | None = None

    def _make_client(self) -> SendGridHTTPClient:
        return SendGridHTTPClient(api_key=self._api_key)

    def _has_credentials(self) -> bool:
        return bool(self._api_key)

    def _ensure_client(self) -> SendGridHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key is present, return InstallResult."""
        if not self._has_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        self.http_client = self._make_client()
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="SendGrid connector installed successfully",
        )

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Call get_stats for today and return HealthCheckResult."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        today = date.today().isoformat()
        try:
            await with_retry(client.get_stats, today, today)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="SendGrid API is reachable",
            )
        except SendGridAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except SendGridNetworkError as exc:
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
        Sync SendGrid marketing contacts and templates into the knowledge base.

        Paginates contacts via _metadata.next / next_page_token.
        """
        kb_id: str = kwargs.get("kb_id", "")
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # ── Contacts ─────────────────────────────────────────────────────────
        page_token: str | None = None
        while True:
            try:
                page = await with_retry(
                    self.http_client.list_contacts,
                    page_size=SYNC_PAGE_SIZE,
                    page_token=page_token,
                )
            except SendGridError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            contacts: list[dict[str, Any]] = page.get("result", []) or []
            found += len(contacts)

            for contact in contacts:
                try:
                    doc = normalize_contact(contact, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            next_token: str | None = (
                page.get("_metadata", {}).get("next")
                or page.get("next_page_token")
            )
            if not next_token or not contacts:
                break
            page_token = next_token

        # ── Templates ─────────────────────────────────────────────────────────
        try:
            tmpl_page = await with_retry(
                self.http_client.list_templates,
                generations="dynamic",
                page_size=SYNC_PAGE_SIZE,
            )
            templates: list[dict[str, Any]] = tmpl_page.get("templates", []) or []
            found += len(templates)
            for template in templates:
                try:
                    doc = normalize_template(template, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except SendGridError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL if synced > 0 else SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Templates sync failed: {exc}",
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

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(self, page_size: int = 1000) -> list[dict[str, Any]]:
        """Return list of contact dicts (first page)."""
        client = self._ensure_client()
        page = await with_retry(client.list_contacts, page_size=page_size)
        return page.get("result", []) or []

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        """Return single contact dict by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_contact, contact_id)

    # ── Lists ──────────────────────────────────────────────────────────────────

    async def list_lists(self) -> list[dict[str, Any]]:
        """Return list of marketing list dicts."""
        client = self._ensure_client()
        page = await with_retry(client.list_lists)
        return page.get("result", []) or []

    # ── Segments ──────────────────────────────────────────────────────────────

    async def list_segments(self) -> list[dict[str, Any]]:
        """Return list of segment dicts."""
        client = self._ensure_client()
        page = await with_retry(client.list_segments)
        return page.get("results", []) or page.get("result", []) or []

    # ── Templates ─────────────────────────────────────────────────────────────

    async def list_templates(self, generations: str = "dynamic") -> list[dict[str, Any]]:
        """Return list of template dicts."""
        client = self._ensure_client()
        page = await with_retry(client.list_templates, generations=generations)
        return page.get("templates", []) or []

    async def get_template(self, template_id: str) -> dict[str, Any]:
        """Return single template dict by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_template, template_id)

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return stats list. Defaults to last 30 days when dates not provided."""
        if end_date is None:
            end_date = date.today().isoformat()
        if start_date is None:
            start_date = (date.today() - timedelta(days=30)).isoformat()
        client = self._ensure_client()
        return await with_retry(client.get_stats, start_date, end_date)

    # ── Suppressions ──────────────────────────────────────────────────────────

    async def list_suppressions(
        self, group_id: int | None = None, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Return list of suppressed emails."""
        client = self._ensure_client()
        return await with_retry(
            client.list_suppressions, group_id=group_id, page_size=page_size
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> "SendGridConnector":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
