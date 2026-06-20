from __future__ import annotations

from typing import Any

from client import PagerDutyHTTPClient
from exceptions import PagerDutyAuthError, PagerDutyError, PagerDutyNetworkError
from helpers import (
    normalize_incident,
    normalize_service,
    normalize_schedule,
    normalize_team,
    normalize_user,
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


CONNECTOR_TYPE: str = "pagerduty"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 100


class PagerDutyConnector(BaseConnector):
    """Shielva connector for PagerDuty incident management and on-call alerting."""

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
            tenant_id=tenant_id, connector_id=connector_id, config=_config
        )
        self._api_key: str = _config.get("api_key", "")
        self.client: PagerDutyHTTPClient = PagerDutyHTTPClient(config=_config)

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._api_key:
            missing.append("api_key")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install credentials — checks api_key is present."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="PagerDuty connector installed successfully.",
        )

    async def health_check(self) -> HealthCheckResult:
        """Verify connectivity by calling GET /abilities."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        try:
            await with_retry(self.client.get_abilities)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="PagerDuty API reachable.",
            )
        except PagerDutyAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except PagerDutyNetworkError as exc:
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

    async def sync(self, kb_id: str = "", **kwargs: Any) -> SyncResult:
        """Sync incidents and services from PagerDuty."""
        found = 0
        synced = 0
        failed = 0

        resource_fetchers = [
            ("incidents", self.list_incidents),
            ("services", self.list_services),
        ]

        for _resource_name, fetcher in resource_fetchers:
            try:
                items = await with_retry(fetcher)
                found += len(items)
                for item in items:
                    try:
                        if kb_id:
                            await self._ingest_document(item, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except PagerDutyError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(
        self, doc: ConnectorDocument, kb_id: str
    ) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Incidents ─────────────────────────────────────────────────────────────

    async def list_incidents(
        self,
        statuses: list[str] | None = None,
        urgencies: list[str] | None = None,
        **kwargs: Any,
    ) -> list[ConnectorDocument]:
        """Fetch all incidents with offset-based pagination, return normalized docs."""
        docs: list[ConnectorDocument] = []
        offset = 0
        while True:
            page = await with_retry(
                self.client.list_incidents,
                statuses=statuses,
                urgencies=urgencies,
                limit=SYNC_PAGE_SIZE,
                offset=offset,
                **kwargs,
            )
            raw_list: list[dict[str, Any]] = page.get("incidents", [])
            for raw in raw_list:
                doc = normalize_incident(raw)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
            if not page.get("more") or not raw_list:
                break
            offset += len(raw_list)
        return docs

    async def get_incident(self, incident_id: str) -> dict[str, Any]:
        """Return a single raw PagerDuty incident by ID."""
        data = await with_retry(self.client.get_incident, incident_id)
        return data.get("incident", data)

    async def list_incident_alerts(
        self, incident_id: str
    ) -> list[dict[str, Any]]:
        """Return raw alert dicts for a given incident."""
        data = await with_retry(self.client.list_incident_alerts, incident_id)
        return data.get("alerts", [])

    # ── Services ──────────────────────────────────────────────────────────────

    async def list_services(self) -> list[ConnectorDocument]:
        """Fetch all services with offset-based pagination, return normalized docs."""
        docs: list[ConnectorDocument] = []
        offset = 0
        while True:
            page = await with_retry(
                self.client.list_services, limit=SYNC_PAGE_SIZE, offset=offset
            )
            raw_list: list[dict[str, Any]] = page.get("services", [])
            for raw in raw_list:
                doc = normalize_service(raw)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
            if not page.get("more") or not raw_list:
                break
            offset += len(raw_list)
        return docs

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(self) -> list[ConnectorDocument]:
        """Fetch all users with offset-based pagination, return normalized docs."""
        docs: list[ConnectorDocument] = []
        offset = 0
        while True:
            page = await with_retry(
                self.client.list_users, limit=SYNC_PAGE_SIZE, offset=offset
            )
            raw_list: list[dict[str, Any]] = page.get("users", [])
            for raw in raw_list:
                doc = normalize_user(raw)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
            if not page.get("more") or not raw_list:
                break
            offset += len(raw_list)
        return docs

    # ── Schedules ─────────────────────────────────────────────────────────────

    async def list_schedules(self) -> list[ConnectorDocument]:
        """Fetch all schedules with offset-based pagination, return normalized docs."""
        docs: list[ConnectorDocument] = []
        offset = 0
        while True:
            page = await with_retry(
                self.client.list_schedules, limit=SYNC_PAGE_SIZE, offset=offset
            )
            raw_list: list[dict[str, Any]] = page.get("schedules", [])
            for raw in raw_list:
                doc = normalize_schedule(raw)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                docs.append(doc)
            if not page.get("more") or not raw_list:
                break
            offset += len(raw_list)
        return docs

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> PagerDutyConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
