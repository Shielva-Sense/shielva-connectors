from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import PipedriveHTTPClient
from exceptions import PipedriveAuthError, PipedriveError, PipedriveNetworkError
from helpers import (
    CircuitBreaker,
    normalize_deal,
    normalize_organization,
    normalize_person,
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
    from shared.base_connector import BaseConnector
    _BASE = BaseConnector
except ImportError:
    _BASE = object  # standalone / test mode

SYNC_PAGE_SIZE = 100
CIRCUIT_BREAKER_THRESHOLD = 5


class PipedriveConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Pipedrive CRM.

    Provides authentication, health checks, full sync, and direct access to
    Pipedrive deals, persons, organizations, and activities via the v1 REST API.
    Authentication uses an API token passed as a query parameter (?api_token=...).
    """

    CONNECTOR_TYPE: str = "pipedrive"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self.tenant_id = tenant_id
        # Pipedrive-specific attrs — accept either "api_token" (ACP install_field key)
        # or "api_key" (legacy / direct instantiation)
        self._api_key: str = _config.get("api_token", "") or _config.get("api_key", "")
        self._company_domain: str = _config.get("company_domain", "")
        self.http_client: PipedriveHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> PipedriveHTTPClient:
        return PipedriveHTTPClient(api_key=self._api_key)

    def _has_credentials(self) -> bool:
        return bool(self._api_key)

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate API key by calling GET /users/me."""
        if not self._has_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required — copy your API token from Pipedrive Settings → Personal preferences → API",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_current_user)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Pipedrive CRM",
            )
        except PipedriveAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Pipedrive authentication failed: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /users/me and return current health."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_current_user)
            await client.aclose()
            self._circuit_breaker.on_success()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Pipedrive API is reachable",
            )
        except PipedriveAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except PipedriveNetworkError as exc:
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
    ) -> SyncResult:
        """
        Sync Pipedrive deals, persons, and organizations into the knowledge base.

        Pipedrive uses offset-based pagination (start + limit).
        full=True / since parameter are accepted for API compatibility — the
        connector always fetches all pages regardless (Pipedrive list APIs do not
        support server-side timestamp filtering on the base list endpoints).
        """
        _ = since
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        for fetch_fn, normalize_fn in (
            (self._fetch_all_deals, normalize_deal),
            (self._fetch_all_persons, normalize_person),
            (self._fetch_all_organizations, normalize_organization),
        ):
            try:
                records = await fetch_fn()
            except PipedriveError as exc:
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

    async def _fetch_all_deals(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        return await self._fetch_all_pages(self.http_client.list_deals, status="all")

    async def _fetch_all_persons(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        return await self._fetch_all_pages(self.http_client.list_persons)

    async def _fetch_all_organizations(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        return await self._fetch_all_pages(self.http_client.list_organizations)

    async def _fetch_all_pages(
        self,
        list_fn: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Paginate through Pipedrive offset-based pages until exhausted."""
        records: list[dict[str, Any]] = []
        start = 0
        while True:
            page = await with_retry(list_fn, limit=SYNC_PAGE_SIZE, start=start, **kwargs)
            data = page.get("data") or []
            if not data:
                break
            records.extend(data)
            pagination = (page.get("additional_data") or {}).get("pagination") or {}
            if not pagination.get("more_items_in_collection", False):
                break
            start += len(data)
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Deals ────────────────────────────────────────────────────────────────

    async def list_deals(
        self,
        status: str = "all",
        limit: int = 100,
        start: int = 0,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_deals, status=status, limit=limit, start=start)

    async def get_deal(self, deal_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_deal, deal_id)

    # ── Persons ──────────────────────────────────────────────────────────────

    async def list_persons(
        self,
        limit: int = 100,
        start: int = 0,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_persons, limit=limit, start=start)

    async def get_person(self, person_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_person, person_id)

    # ── Organizations ────────────────────────────────────────────────────────

    async def list_organizations(
        self,
        limit: int = 100,
        start: int = 0,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_organizations, limit=limit, start=start)

    # ── Activities ───────────────────────────────────────────────────────────

    async def list_activities(
        self,
        limit: int = 100,
        start: int = 0,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_activities, limit=limit, start=start)

    # ── Pipelines ────────────────────────────────────────────────────────────

    async def list_pipelines(self) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_pipelines)

    # ── Stages ───────────────────────────────────────────────────────────────

    async def list_stages(self) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_stages)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> PipedriveHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> PipedriveConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
