from __future__ import annotations

from datetime import datetime
from typing import Any

from client import MarketoHTTPClient
from exceptions import MarketoAuthError, MarketoError, MarketoNetworkError
from helpers import (
    normalize_campaign,
    normalize_lead,
    normalize_list,
    normalize_program,
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
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

CONNECTOR_TYPE = "marketo"
AUTH_TYPE = "oauth2"

SYNC_PAGE_SIZE = 300  # Marketo max per page for leads


class MarketoConnector(BaseConnector):
    """
    Shielva connector for Marketo (Adobe marketing automation).

    Provides OAuth 2.0 Client Credentials authentication, health checks,
    full sync of leads / lists / campaigns / programs, and direct record access
    via the Marketo REST API.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(
            tenant_id=tenant_id,
            connector_id=connector_id,
            config=_config,
        )
        self.client = MarketoHTTPClient(config=_config)

    def _has_credentials(self) -> bool:
        cfg = self.config
        return bool(
            cfg.get("client_id")
            and cfg.get("client_secret")
            and cfg.get("munchkin_id")
        )

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by probing GET /leads.json with minimal params."""
        if not self._has_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id, client_secret, and munchkin_id are all required",
            )
        client = MarketoHTTPClient(config=self.config)
        try:
            await with_retry(client.get_leads_probe)
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Marketo",
            )
        except MarketoAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Marketo authentication failed: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping Marketo leads probe and return current health."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id, client_secret, and munchkin_id are required",
            )
        client = MarketoHTTPClient(config=self.config)
        try:
            await with_retry(client.get_leads_probe)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Marketo REST API is reachable",
            )
        except MarketoAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except MarketoNetworkError as exc:
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
        """Sync Marketo leads, lists, campaigns, and programs into the knowledge base."""
        _ = full, since  # Marketo uses cursor/offset pagination; no server-side delta filter on list

        found = 0
        synced = 0
        failed = 0

        fetch_pairs: list[tuple[Any, Any]] = [
            (self._fetch_all_leads, normalize_lead),
            (self._fetch_all_lists, normalize_list),
            (self._fetch_all_campaigns, normalize_campaign),
            (self._fetch_all_programs, normalize_program),
        ]

        for fetch_fn, normalize_fn in fetch_pairs:
            try:
                records = await fetch_fn()
            except MarketoError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            found += len(records)
            for record in records:
                try:
                    doc = normalize_fn(record, self.connector_id, self.tenant_id)
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

    # ── Internal fetch helpers ────────────────────────────────────────────────

    async def _fetch_all_leads(self) -> list[dict[str, Any]]:
        """Page through all leads using nextPageToken cursor."""
        records: list[dict[str, Any]] = []
        # Initial request: filter by id >= 1 to get all leads (common pattern)
        page = await with_retry(
            self.client.get_leads,
            filter_type="id",
            filter_values=["1"],
            fields=["id", "firstName", "lastName", "email", "company", "title", "phone", "createdAt", "updatedAt"],
        )
        records.extend(page.get("result", []))
        next_token: str | None = page.get("nextPageToken")
        while next_token:
            page = await with_retry(
                self.client.get_leads,
                next_page_token=next_token,
            )
            records.extend(page.get("result", []))
            next_token = page.get("nextPageToken")
        return records

    async def _fetch_all_lists(self) -> list[dict[str, Any]]:
        """Page through all static lists using nextPageToken."""
        records: list[dict[str, Any]] = []
        page = await with_retry(self.client.get_lists)
        records.extend(page.get("result", []))
        next_token: str | None = page.get("nextPageToken")
        while next_token:
            page = await with_retry(self.client.get_lists, next_page_token=next_token)
            records.extend(page.get("result", []))
            next_token = page.get("nextPageToken")
        return records

    async def _fetch_all_campaigns(self) -> list[dict[str, Any]]:
        """Page through all campaigns using nextPageToken."""
        records: list[dict[str, Any]] = []
        page = await with_retry(self.client.get_campaigns)
        records.extend(page.get("result", []))
        next_token: str | None = page.get("nextPageToken")
        while next_token:
            page = await with_retry(
                self.client.get_campaigns, next_page_token=next_token
            )
            records.extend(page.get("result", []))
            next_token = page.get("nextPageToken")
        return records

    async def _fetch_all_programs(self) -> list[dict[str, Any]]:
        """Page through all programs using offset-based pagination."""
        records: list[dict[str, Any]] = []
        offset = 0
        max_return = 200
        while True:
            page = await with_retry(
                self.client.get_programs,
                offset=offset,
                max_return=max_return,
            )
            batch = page.get("result", [])
            records.extend(batch)
            if len(batch) < max_return:
                break
            offset += max_return
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Public resource methods ───────────────────────────────────────────────

    async def list_leads(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all leads (full paginated fetch)."""
        return await self._fetch_all_leads()

    async def list_lists(self) -> list[dict[str, Any]]:
        """Return all static lists."""
        return await self._fetch_all_lists()

    async def list_campaigns(self) -> list[dict[str, Any]]:
        """Return all campaigns."""
        return await self._fetch_all_campaigns()

    async def list_programs(self) -> list[dict[str, Any]]:
        """Return all programs."""
        return await self._fetch_all_programs()

    async def get_lead(self, lead_id: int) -> dict[str, Any]:
        """Retrieve a single lead by ID."""
        return await with_retry(self.client.get_lead, lead_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> MarketoConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
