from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import FreshdeskHTTPClient
from exceptions import FreshdeskAuthError, FreshdeskError, FreshdeskNetworkError
from helpers import normalize_contact, normalize_ticket, with_retry
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

CONNECTOR_TYPE: str = "freshdesk"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 100


class FreshdeskConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Freshdesk.

    Syncs support tickets (with conversations), contacts, and agents from
    a Freshdesk account using API Key (HTTP Basic) authentication.
    """

    CONNECTOR_TYPE: str = "freshdesk"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._tenant_id = tenant_id

        self._domain: str = _config.get("domain", "").strip().rstrip("/")
        self._api_key: str = _config.get("api_key", "").strip()
        self._http_client: FreshdeskHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> FreshdeskHTTPClient:
        return FreshdeskHTTPClient()

    def _ensure_client(self) -> FreshdeskHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not self._domain or not self._api_key

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate domain + api_key by calling GET /api/v2/agents/me."""
        if self._missing_creds():
            missing = []
            if not self._domain:
                missing.append("domain")
            if not self._api_key:
                missing.append("api_key")
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            agent = await with_retry(
                client.get_current_agent, self._domain, self._api_key
            )
            await client.aclose()
            agent_name: str = agent.get("contact", {}).get("name", "") or agent.get(
                "name", ""
            ) or "Unknown agent"
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Freshdesk as {agent_name}",
            )
        except FreshdeskAuthError as exc:
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
        """Ping GET /api/v2/agents/me and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="domain and api_key are required",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_current_agent, self._domain, self._api_key)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Freshdesk API is reachable",
            )
        except FreshdeskAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except FreshdeskNetworkError as exc:
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
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync tickets and contacts from Freshdesk.

        full=True → fetch all records (no date filter).
        since=<datetime> → fetch records updated after that timestamp.
        """
        if self._http_client is None:
            self._http_client = self._make_client()

        updated_since: str | None = None
        if not full and since:
            updated_since = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        found = 0
        synced = 0
        failed = 0

        # Sync tickets
        try:
            ticket_page = 1
            while True:
                tickets = await with_retry(
                    self._http_client.list_tickets,
                    self._domain,
                    self._api_key,
                    page=ticket_page,
                    per_page=SYNC_PAGE_SIZE,
                    updated_since=updated_since,
                )
                if not tickets:
                    break
                found += len(tickets)

                for ticket in tickets:
                    try:
                        ticket_id: int = ticket.get("id", 0)
                        conversations: list[dict[str, Any]] = []
                        try:
                            conversations = await with_retry(
                                self._http_client.list_ticket_conversations,
                                self._domain,
                                self._api_key,
                                ticket_id,
                            )
                        except Exception:
                            pass  # conversations are best-effort
                        doc = normalize_ticket(
                            ticket,
                            conversations,
                            self.connector_id,
                            self._tenant_id,
                            self._domain,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                ticket_page += 1
        except FreshdeskError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # Sync contacts
        try:
            contact_page = 1
            while True:
                contacts = await with_retry(
                    self._http_client.list_contacts,
                    self._domain,
                    self._api_key,
                    page=contact_page,
                    per_page=SYNC_PAGE_SIZE,
                    updated_since=updated_since,
                )
                if not contacts:
                    break
                found += len(contacts)

                for contact in contacts:
                    try:
                        doc = normalize_contact(
                            contact,
                            self.connector_id,
                            self._tenant_id,
                            self._domain,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                contact_page += 1
        except FreshdeskError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Contacts sync failed: {exc}",
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

    # ── Ticket methods ────────────────────────────────────────────────────────

    async def list_tickets(
        self,
        page: int = 1,
        per_page: int = 100,
        updated_since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one page of tickets; empty list signals end of pagination."""
        client = self._ensure_client()
        return await with_retry(
            client.list_tickets,
            self._domain,
            self._api_key,
            page=page,
            per_page=per_page,
            updated_since=updated_since,
        )

    async def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        """Return a single ticket and its conversations as a combined dict."""
        client = self._ensure_client()
        ticket = await with_retry(
            client.get_ticket, self._domain, self._api_key, ticket_id
        )
        conversations: list[dict[str, Any]] = []
        try:
            conversations = await with_retry(
                client.list_ticket_conversations,
                self._domain,
                self._api_key,
                ticket_id,
            )
        except Exception:
            pass
        return {**ticket, "conversations": conversations}

    # ── Contact methods ───────────────────────────────────────────────────────

    async def list_contacts(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one page of contacts; empty list signals end of pagination."""
        client = self._ensure_client()
        return await with_retry(
            client.list_contacts,
            self._domain,
            self._api_key,
            page=page,
            per_page=per_page,
        )

    async def get_contact(self, contact_id: int) -> dict[str, Any]:
        """Return a single contact by ID."""
        client = self._ensure_client()
        return await with_retry(
            client.get_contact, self._domain, self._api_key, contact_id
        )

    # ── Agent methods ─────────────────────────────────────────────────────────

    async def list_agents(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one page of agents; empty list signals end of pagination."""
        client = self._ensure_client()
        return await with_retry(
            client.list_agents,
            self._domain,
            self._api_key,
            page=page,
            per_page=per_page,
        )

    # ── Company methods ───────────────────────────────────────────────────────

    async def list_companies(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one page of companies; empty list signals end of pagination."""
        client = self._ensure_client()
        return await with_retry(
            client.list_companies,
            self._domain,
            self._api_key,
            page=page,
            per_page=per_page,
        )

    # ── Group methods ─────────────────────────────────────────────────────────

    async def list_groups(self) -> list[dict[str, Any]]:
        """Return all support groups."""
        client = self._ensure_client()
        return await with_retry(
            client.list_groups,
            self._domain,
            self._api_key,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> FreshdeskConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
