from __future__ import annotations

from datetime import datetime
from typing import Any

from client.http_client import FreshserviceHTTPClient
from exceptions import (
    FreshserviceAuthError,
    FreshserviceError,
    FreshserviceNetworkError,
)
from helpers.utils import (
    normalize_agent,
    normalize_asset,
    normalize_change,
    normalize_ticket,
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


CONNECTOR_TYPE: str = "freshservice"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 100


class FreshserviceConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Freshservice (IT Service Management).

    Syncs ITSM tickets, CMDB assets, agents, groups, service catalog items,
    and change requests from a Freshservice account using API Key (HTTP Basic)
    authentication.

    Auth: HTTP Basic with api_key as username, "X" as password.
    Install fields: api_key (password), subdomain (string, e.g. "mycompany").
    API base: https://{subdomain}.freshservice.com/api/v2/
    """

    CONNECTOR_TYPE: str = "freshservice"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if not isinstance(BaseConnector, type) or BaseConnector is not object:
            try:
                super().__init__(
                    tenant_id=tenant_id,
                    connector_id=connector_id,
                    config=_config,
                )
            except TypeError:
                self.tenant_id = tenant_id
                self.connector_id = connector_id
                self.config = _config
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = _config

        self._api_key: str = _config.get("api_key", "").strip()
        self._subdomain: str = _config.get("subdomain", "").strip().rstrip("/")
        self.client: FreshserviceHTTPClient = FreshserviceHTTPClient(config=_config)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _missing_creds(self) -> bool:
        return not self._api_key or not self._subdomain

    def _missing_field_names(self) -> list[str]:
        missing: list[str] = []
        if not self._api_key:
            missing.append("api_key")
        if not self._subdomain:
            missing.append("subdomain")
        return missing

    def _make_client(self) -> FreshserviceHTTPClient:
        return FreshserviceHTTPClient(config=self.config)

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key + subdomain by calling GET /api/v2/agents?per_page=1."""
        if self._missing_creds():
            missing = self._missing_field_names()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            result = await with_retry(
                client.get_agents,
                per_page=1,
            )
            await client.aclose()
            agents: list[dict[str, Any]] = result.get("agents", [])
            agent_count = len(agents)
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=(
                    f"Connected to Freshservice ({self._subdomain}). "
                    f"API responding with {agent_count} agent(s) visible."
                ),
            )
        except FreshserviceAuthError as exc:
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
        """Ping GET /api/v2/agents?per_page=1 and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key and subdomain are required",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_agents, per_page=1)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Freshservice API is reachable",
            )
        except FreshserviceAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except FreshserviceNetworkError as exc:
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
        **kwargs: Any,
    ) -> SyncResult:
        """
        Sync tickets, assets, agents, and changes from Freshservice.

        full=True  → fetch all records (no date filter).
        since=<dt> → fetch records updated after that timestamp (incremental).
        kb_id      → knowledge base ID to ingest into (passed to _ingest_document).
        """
        updated_since: str | None = None
        if not full and since:
            updated_since = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        found = 0
        synced = 0
        failed = 0

        # ── Tickets ──
        try:
            ticket_page = 1
            while True:
                resp = await with_retry(
                    self.client.get_tickets,
                    page=ticket_page,
                    per_page=SYNC_PAGE_SIZE,
                    updated_since=updated_since,
                )
                tickets: list[dict[str, Any]] = resp.get("tickets", [])
                if not tickets:
                    break
                found += len(tickets)

                for ticket in tickets:
                    try:
                        doc = normalize_ticket(
                            ticket,
                            connector_id=self.connector_id,
                            tenant_id=self.tenant_id,
                            subdomain=self._subdomain,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                ticket_page += 1
        except FreshserviceError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # ── Assets ──
        try:
            asset_page = 1
            while True:
                resp = await with_retry(
                    self.client.get_assets,
                    page=asset_page,
                    per_page=SYNC_PAGE_SIZE,
                )
                assets: list[dict[str, Any]] = resp.get("assets", [])
                if not assets:
                    break
                found += len(assets)

                for asset in assets:
                    try:
                        doc = normalize_asset(
                            asset,
                            connector_id=self.connector_id,
                            tenant_id=self.tenant_id,
                            subdomain=self._subdomain,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                asset_page += 1
        except FreshserviceError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Asset sync failed: {exc}",
            )

        # ── Agents ──
        try:
            agent_page = 1
            while True:
                resp = await with_retry(
                    self.client.get_agents,
                    page=agent_page,
                    per_page=SYNC_PAGE_SIZE,
                )
                agents: list[dict[str, Any]] = resp.get("agents", [])
                if not agents:
                    break
                found += len(agents)

                for agent in agents:
                    try:
                        doc = normalize_agent(
                            agent,
                            connector_id=self.connector_id,
                            tenant_id=self.tenant_id,
                            subdomain=self._subdomain,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                agent_page += 1
        except FreshserviceError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Agent sync failed: {exc}",
            )

        # ── Changes ──
        try:
            change_page = 1
            while True:
                resp = await with_retry(
                    self.client.get_changes,
                    page=change_page,
                    per_page=SYNC_PAGE_SIZE,
                    updated_since=updated_since,
                )
                changes: list[dict[str, Any]] = resp.get("changes", [])
                if not changes:
                    break
                found += len(changes)

                for change in changes:
                    try:
                        doc = normalize_change(
                            change,
                            connector_id=self.connector_id,
                            tenant_id=self.tenant_id,
                            subdomain=self._subdomain,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                change_page += 1
        except FreshserviceError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Change sync failed: {exc}",
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
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return one page of ITSM tickets; empty list signals end of pagination."""
        resp = await with_retry(
            self.client.get_tickets,
            page=page,
            per_page=per_page,
            updated_since=updated_since,
        )
        return resp.get("tickets", [])

    async def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        """Return a single ITSM ticket by ID."""
        return await with_retry(self.client.get_ticket, ticket_id)

    # ── Asset methods ─────────────────────────────────────────────────────────

    async def list_assets(
        self,
        page: int = 1,
        per_page: int = 100,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return one page of CMDB assets; empty list signals end of pagination."""
        resp = await with_retry(
            self.client.get_assets,
            page=page,
            per_page=per_page,
        )
        return resp.get("assets", [])

    # ── Agent methods ─────────────────────────────────────────────────────────

    async def list_agents(
        self,
        page: int = 1,
        per_page: int = 100,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return one page of agents; empty list signals end of pagination."""
        resp = await with_retry(
            self.client.get_agents,
            page=page,
            per_page=per_page,
        )
        return resp.get("agents", [])

    # ── Change methods ────────────────────────────────────────────────────────

    async def list_changes(
        self,
        page: int = 1,
        per_page: int = 100,
        updated_since: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return one page of change requests; empty list signals end of pagination."""
        resp = await with_retry(
            self.client.get_changes,
            page=page,
            per_page=per_page,
            updated_since=updated_since,
        )
        return resp.get("changes", [])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> FreshserviceConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
