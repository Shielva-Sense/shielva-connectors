from __future__ import annotations

from typing import Any

from client import ReamazeHTTPClient
from exceptions import ReamazeAuthError, ReamazeError, ReamazeNetworkError
from helpers import normalize_article, normalize_contact, normalize_conversation, with_retry
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
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

CONNECTOR_TYPE: str = "reamaze"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 50


class ReamazeConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Re:amaze.

    Syncs conversations, contacts, knowledge-base articles, and reports
    from a Re:amaze account using HTTP Basic Auth (email + API token).
    API base: https://{brand_subdomain}.reamaze.com/api/v1/
    """

    CONNECTOR_TYPE: str = "reamaze"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if not isinstance(BaseConnector, type) or BaseConnector.__name__ != "BaseConnector":
            super().__init__(
                tenant_id=tenant_id, connector_id=connector_id, config=_config
            )
        else:
            try:
                super().__init__(
                    tenant_id=tenant_id, connector_id=connector_id, config=_config
                )
            except Exception:
                self.config = _config
                self.connector_id = connector_id
                self.tenant_id = tenant_id

        self._brand_subdomain: str = _config.get("brand_subdomain", "").strip()
        self._email: str = _config.get("email", "").strip()
        self._api_token: str = _config.get("api_token", "").strip()
        self._http_client: ReamazeHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> ReamazeHTTPClient:
        return ReamazeHTTPClient(config=self.config)

    def _ensure_client(self) -> ReamazeHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not self._brand_subdomain or not self._email or not self._api_token

    def _missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self._brand_subdomain:
            missing.append("brand_subdomain")
        if not self._email:
            missing.append("email")
        if not self._api_token:
            missing.append("api_token")
        return missing

    def _enrich(self, doc: ConnectorDocument) -> ConnectorDocument:
        """Stamp connector_id and tenant_id on a normalized document."""
        doc.connector_id = self.connector_id
        doc.tenant_id = self.tenant_id
        return doc

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling GET /people?page=1."""
        missing = self._missing_fields()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            result = await with_retry(client.get_people, page=1)
            await client.aclose()
            contact_count: int = len(result.get("contacts", []))
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=(
                    f"Connected to Re:amaze ({self._brand_subdomain}.reamaze.com). "
                    f"Found {contact_count} contact(s) on first page."
                ),
            )
        except ReamazeAuthError as exc:
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
        """Ping GET /people?page=1 and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="brand_subdomain, email, and api_token are required",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_people, page=1)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Re:amaze API is reachable",
            )
        except ReamazeAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ReamazeNetworkError as exc:
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

    async def sync(self, kb_id: str = "", **kwargs: Any) -> SyncResult:
        """
        Sync conversations, contacts, and articles from Re:amaze.

        Iterates all pages for each resource type. Documents are optionally
        pushed to the knowledge base when kb_id is provided.
        """
        if self._http_client is None:
            self._http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync conversations
        try:
            page = 1
            while True:
                resp = await with_retry(self._http_client.get_conversations, page=page)
                conversations: list[dict[str, Any]] = resp.get("conversations", [])
                total_pages: int = resp.get("total_pages", 1) or 1
                if not conversations:
                    break
                found += len(conversations)
                for raw in conversations:
                    try:
                        doc = self._enrich(normalize_conversation(raw))
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if page >= total_pages:
                    break
                page += 1
        except ReamazeError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # Sync contacts
        try:
            page = 1
            while True:
                resp = await with_retry(self._http_client.get_people, page=page)
                contacts: list[dict[str, Any]] = resp.get("contacts", [])
                total_pages = resp.get("total_pages", 1) or 1
                if not contacts:
                    break
                found += len(contacts)
                for raw in contacts:
                    try:
                        doc = self._enrich(normalize_contact(raw))
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if page >= total_pages:
                    break
                page += 1
        except ReamazeError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Contacts sync failed: {exc}",
            )

        # Sync articles
        try:
            page = 1
            while True:
                resp = await with_retry(self._http_client.get_articles, page=page)
                articles: list[dict[str, Any]] = resp.get("articles", [])
                total_pages = resp.get("total_pages", 1) or 1
                if not articles:
                    break
                found += len(articles)
                for raw in articles:
                    try:
                        doc = self._enrich(normalize_article(raw))
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if page >= total_pages:
                    break
                page += 1
        except ReamazeError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Articles sync failed: {exc}",
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

    # ── Conversation methods ──────────────────────────────────────────────────

    async def list_conversations(self, page: int = 1, **kwargs: Any) -> list[dict[str, Any]]:
        """Return one page of conversations."""
        client = self._ensure_client()
        resp = await with_retry(client.get_conversations, page=page, **kwargs)
        return resp.get("conversations", [])

    async def get_conversation(self, slug: str) -> dict[str, Any]:
        """Return a single conversation by slug."""
        client = self._ensure_client()
        return await with_retry(client.get_conversation, slug)

    # ── Contact methods ───────────────────────────────────────────────────────

    async def list_contacts(self, page: int = 1, **kwargs: Any) -> list[dict[str, Any]]:
        """Return one page of contacts."""
        client = self._ensure_client()
        resp = await with_retry(client.get_people, page=page, **kwargs)
        return resp.get("contacts", [])

    async def get_contact(self, contact_id: int) -> dict[str, Any]:
        """Return a single contact by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_person, contact_id)

    # ── Article methods ───────────────────────────────────────────────────────

    async def list_articles(self, page: int = 1, **kwargs: Any) -> list[dict[str, Any]]:
        """Return one page of knowledge base articles."""
        client = self._ensure_client()
        resp = await with_retry(client.get_articles, page=page, **kwargs)
        return resp.get("articles", [])

    # ── Report methods ────────────────────────────────────────────────────────

    async def get_report_summary(self) -> dict[str, Any]:
        """Return report summary statistics."""
        client = self._ensure_client()
        return await with_retry(client.get_report_summary)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> ReamazeConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
