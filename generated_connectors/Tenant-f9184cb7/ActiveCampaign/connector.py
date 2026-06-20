from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import ActiveCampaignHTTPClient
from exceptions import (
    ActiveCampaignAuthError,
    ActiveCampaignError,
    ActiveCampaignNetworkError,
)
from helpers import (
    CircuitBreaker,
    normalize_campaign,
    normalize_contact,
    normalize_deal,
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

CONNECTOR_TYPE: str = "activecampaign"
SYNC_PAGE_SIZE: int = 100
CIRCUIT_BREAKER_THRESHOLD: int = 5


class ActiveCampaignConnector(BaseConnector):
    """
    Shielva connector for ActiveCampaign.

    Provides authentication, health checks, full sync, and direct access to
    ActiveCampaign contacts, deals, campaigns, automations, lists, and tags
    via the REST API v3.  Authentication uses an API key passed in the
    Api-Token header.

    Config keys:
        api_key      (required) — ActiveCampaign API key.
        account_name (required) — The subdomain part of the ActiveCampaign URL
                                  (e.g. "mycompany" for mycompany.api-activecampaign.com).
    """

    CONNECTOR_TYPE: str = "activecampaign"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(
            tenant_id=tenant_id,
            connector_id=connector_id,
            config=_config,
        )
        self._api_key: str = _config.get("api_key", "")
        self._account_name: str = _config.get("account_name", "")
        self.http_client: ActiveCampaignHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=CIRCUIT_BREAKER_THRESHOLD
        )

    def _make_client(self) -> ActiveCampaignHTTPClient:
        return ActiveCampaignHTTPClient(
            api_key=self._api_key,
            account_name=self._account_name,
        )

    def _has_credentials(self) -> bool:
        return bool(self._api_key and self._account_name)

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling GET /users/me."""
        if not self._api_key and not self._account_name:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key and account_name are required",
            )
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        if not self._account_name:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="account_name is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_me)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to ActiveCampaign",
            )
        except ActiveCampaignAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"ActiveCampaign authentication failed: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /users/me and return current health with user name/email."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key and account_name are required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_me)
            await client.aclose()
            self._circuit_breaker.on_success()
            user = data.get("user", {})
            user_name = (
                f"{user.get('firstName', '')} {user.get('lastName', '')}".strip()
            )
            user_email = user.get("email", "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="ActiveCampaign API is reachable",
                user_name=user_name,
                user_email=user_email,
            )
        except ActiveCampaignAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ActiveCampaignNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = (
                ConnectorHealth.DEGRADED
                if not self._circuit_breaker.is_open
                else ConnectorHealth.OFFLINE
            )
            return HealthCheckResult(
                health=health,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """
        Sync ActiveCampaign contacts, deals, and campaigns into the knowledge base.

        Uses offset-based pagination (limit + offset). AC meta.total gives the total count.
        The ``since`` parameter is accepted for API compatibility but ignored — AC list
        endpoints do not support server-side timestamp filtering.
        """
        _ = since, full  # AC list endpoints have no server-side timestamp filter
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        for fetch_fn, normalize_fn in (
            (self._fetch_all_contacts, normalize_contact),
            (self._fetch_all_deals, normalize_deal),
            (self._fetch_all_campaigns, normalize_campaign),
        ):
            try:
                records = await fetch_fn()
            except ActiveCampaignError as exc:
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

    async def _fetch_all_contacts(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await with_retry(
                self.http_client.list_contacts,
                limit=SYNC_PAGE_SIZE,
                offset=offset,
            )
            batch = page.get("contacts", [])
            records.extend(batch)
            total = int(page.get("meta", {}).get("total", 0))
            offset += len(batch)
            if not batch or offset >= total:
                break
        return records

    async def _fetch_all_deals(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await with_retry(
                self.http_client.list_deals,
                limit=SYNC_PAGE_SIZE,
                offset=offset,
            )
            batch = page.get("deals", [])
            records.extend(batch)
            total = int(page.get("meta", {}).get("total", 0))
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
                self.http_client.list_campaigns,
                limit=SYNC_PAGE_SIZE,
                offset=offset,
            )
            batch = page.get("campaigns", [])
            records.extend(batch)
            total = int(page.get("meta", {}).get("total", 0))
            offset += len(batch)
            if not batch or offset >= total:
                break
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def list_contacts(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_contacts, limit=limit, offset=offset)

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_contact, contact_id)

    # ── Lists ────────────────────────────────────────────────────────────────

    async def list_lists(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_lists, limit=limit, offset=offset)

    # ── Campaigns ────────────────────────────────────────────────────────────

    async def list_campaigns(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_campaigns, limit=limit, offset=offset)

    # ── Automations ──────────────────────────────────────────────────────────

    async def list_automations(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_automations, limit=limit, offset=offset)

    # ── Deals ────────────────────────────────────────────────────────────────

    async def list_deals(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_deals, limit=limit, offset=offset)

    async def get_deal(self, deal_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_deal, deal_id)

    # ── Tags ─────────────────────────────────────────────────────────────────

    async def list_tags(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_tags, limit=limit, offset=offset)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> ActiveCampaignHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> ActiveCampaignConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
