from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import ZendeskHTTPClient
from exceptions import ZendeskAuthError, ZendeskError, ZendeskNetworkError
from helpers import normalize_ticket, with_retry
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

SYNC_PAGE_SIZE: int = 100
CONNECTOR_TYPE: str = "zendesk"
AUTH_TYPE: str = "api_key"


class ZendeskConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Zendesk Support.

    Syncs tickets, ticket comments, users, organizations, and macros from the
    Zendesk Support REST API v2 using Basic Auth (email/token pattern).
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
        # Convenience keyword args for standalone / test usage
        subdomain: str = "",
        email: str = "",
        api_token: str = "",
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._tenant_id: str = tenant_id

        self._subdomain: str = _config.get("subdomain", "") or subdomain
        self._email: str = _config.get("email", "") or email
        self._api_token: str = _config.get("api_token", "") or api_token
        self._http_client: ZendeskHTTPClient | None = None

    def _make_client(self) -> ZendeskHTTPClient:
        return ZendeskHTTPClient()

    def _ensure_client(self) -> ZendeskHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._subdomain:
            missing.append("subdomain")
        if not self._email:
            missing.append("email")
        if not self._api_token:
            missing.append("api_token")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install credentials by calling GET /users/me.json."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_current_user,
                self._subdomain,
                self._email,
                self._api_token,
            )
            user = data.get("user", {})
            agent_name: str = user.get("name", "")
            agent_email: str = user.get("email", "")
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Zendesk as {agent_name} ({agent_email})",
            )
        except ZendeskAuthError as exc:
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
        """Ping GET /users/me.json and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_current_user,
                self._subdomain,
                self._email,
                self._api_token,
            )
            user = data.get("user", {})
            agent_name: str = user.get("name", "")
            agent_email: str = user.get("email", "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Zendesk API reachable. Agent: {agent_name} ({agent_email})",
            )
        except ZendeskAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ZendeskNetworkError as exc:
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
        **kwargs: Any,
    ) -> SyncResult:
        """Sync all Zendesk tickets (with comments) into the knowledge base.

        full=True → fetch all tickets regardless of updated_at.
        since=<datetime> → only tickets updated after that timestamp (incremental).
        """
        client = self._ensure_client()

        updated_after: str | None = None
        if not full and since:
            updated_after = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        found = 0
        synced = 0
        failed = 0
        page = 1

        while True:
            try:
                page_data = await with_retry(
                    client.list_tickets,
                    self._subdomain,
                    self._email,
                    self._api_token,
                    page=page,
                    per_page=SYNC_PAGE_SIZE,
                    updated_after=updated_after,
                )
            except ZendeskError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            tickets: list[dict[str, Any]] = page_data.get("tickets", [])
            found += len(tickets)

            for ticket in tickets:
                try:
                    ticket_id: int = ticket["id"]
                    comments_data = await with_retry(
                        client.list_ticket_comments,
                        self._subdomain,
                        self._email,
                        self._api_token,
                        ticket_id,
                    )
                    comments: list[dict[str, Any]] = comments_data.get("comments", [])

                    doc = normalize_ticket(
                        ticket,
                        comments,
                        self.connector_id,
                        self._tenant_id,
                        self._subdomain,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # Zendesk cursor-based pagination: next_page is None when done
            if not page_data.get("next_page") or not tickets:
                break
            page += 1

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

    # ── Tickets ───────────────────────────────────────────────────────────────

    async def list_tickets(
        self,
        status: str | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        per_page: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Return a list of Zendesk ticket dicts, following next_page pagination."""
        client = self._ensure_client()
        all_tickets: list[dict[str, Any]] = []
        current_page = page

        while True:
            params_kwargs: dict[str, Any] = dict(
                page=current_page,
                per_page=per_page,
                sort_by=sort_by,
                sort_order=sort_order,
            )
            if status:
                params_kwargs["status"] = status
            page_data = await with_retry(
                client.list_tickets,
                self._subdomain,
                self._email,
                self._api_token,
                **params_kwargs,
            )
            tickets: list[dict[str, Any]] = page_data.get("tickets", [])
            all_tickets.extend(tickets)
            if not page_data.get("next_page") or not tickets:
                break
            current_page += 1

        return all_tickets

    async def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        """Return a single ticket dict (Zendesk envelope: {ticket: {...}})."""
        client = self._ensure_client()
        return await with_retry(
            client.get_ticket,
            self._subdomain,
            self._email,
            self._api_token,
            ticket_id,
        )

    async def list_ticket_comments(self, ticket_id: int) -> list[dict[str, Any]]:
        """Return a list of comment dicts for the given ticket."""
        client = self._ensure_client()
        data = await with_retry(
            client.list_ticket_comments,
            self._subdomain,
            self._email,
            self._api_token,
            ticket_id,
        )
        return data.get("comments", [])

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(
        self,
        role: str | None = None,
        per_page: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Return a list of Zendesk user dicts, following next_page pagination."""
        client = self._ensure_client()
        all_users: list[dict[str, Any]] = []
        current_page = page

        while True:
            params_kwargs: dict[str, Any] = dict(page=current_page, per_page=per_page)
            if role:
                params_kwargs["role"] = role
            page_data = await with_retry(
                client.list_users,
                self._subdomain,
                self._email,
                self._api_token,
                **params_kwargs,
            )
            users: list[dict[str, Any]] = page_data.get("users", [])
            all_users.extend(users)
            if not page_data.get("next_page") or not users:
                break
            current_page += 1

        return all_users

    async def get_user(self, user_id: int) -> dict[str, Any]:
        """Return a single user dict (Zendesk envelope: {user: {...}})."""
        client = self._ensure_client()
        return await with_retry(
            client.get_user,
            self._subdomain,
            self._email,
            self._api_token,
            user_id,
        )

    # ── Organizations ─────────────────────────────────────────────────────────

    async def list_organizations(
        self,
        per_page: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Return a list of Zendesk organization dicts, following next_page pagination."""
        client = self._ensure_client()
        all_orgs: list[dict[str, Any]] = []
        current_page = page

        while True:
            page_data = await with_retry(
                client.list_organizations,
                self._subdomain,
                self._email,
                self._api_token,
                page=current_page,
                per_page=per_page,
            )
            orgs: list[dict[str, Any]] = page_data.get("organizations", [])
            all_orgs.extend(orgs)
            if not page_data.get("next_page") or not orgs:
                break
            current_page += 1

        return all_orgs

    # ── Macros ────────────────────────────────────────────────────────────────

    async def list_macros(
        self,
        per_page: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Return a list of Zendesk macro dicts, following next_page pagination."""
        client = self._ensure_client()
        all_macros: list[dict[str, Any]] = []
        current_page = page

        while True:
            page_data = await with_retry(
                client.list_macros,
                self._subdomain,
                self._email,
                self._api_token,
                page=current_page,
                per_page=per_page,
            )
            macros: list[dict[str, Any]] = page_data.get("macros", [])
            all_macros.extend(macros)
            if not page_data.get("next_page") or not macros:
                break
            current_page += 1

        return all_macros

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> ZendeskConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
