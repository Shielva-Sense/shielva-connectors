from __future__ import annotations

from typing import Any

from client import ServiceNowHTTPClient
from exceptions import ServiceNowAuthError, ServiceNowError, ServiceNowNetworkError
from helpers import normalize_change, normalize_incident, with_retry
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
CONNECTOR_TYPE: str = "servicenow"
AUTH_TYPE: str = "api_key"


class ServiceNowConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for ServiceNow.

    Syncs incidents and change requests from the ServiceNow Table REST API
    using HTTP Basic authentication (username + password).

    Install fields: username, password, instance.
    API base: https://{instance}.service-now.com/api/now/
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
        # Convenience keyword args for standalone / test usage
        instance: str = "",
        username: str = "",
        password: str = "",
    ) -> None:
        _config = config or {}
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

        self._instance: str = _config.get("instance", "") or instance
        self._username: str = _config.get("username", "") or username
        self._password: str = _config.get("password", "") or password
        self._http_client: ServiceNowHTTPClient | None = None

    def _make_client(self) -> ServiceNowHTTPClient:
        return ServiceNowHTTPClient()

    def _ensure_client(self) -> ServiceNowHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._instance:
            missing.append("instance")
        if not self._username:
            missing.append("username")
        if not self._password:
            missing.append("password")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install credentials by calling GET /api/now/table/sys_user?sysparm_limit=1."""
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
                self._instance,
                self._username,
                self._password,
            )
            results: list[dict[str, Any]] = data.get("result", [])
            user_name: str = ""
            if results:
                first = results[0]
                user_name = (
                    first.get("name", "")
                    or first.get("user_name", "")
                    or ""
                )
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to ServiceNow instance '{self._instance}'"
                + (f" as {user_name}" if user_name else ""),
            )
        except ServiceNowAuthError as exc:
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
        """Ping GET /api/now/table/sys_user?sysparm_limit=1 and return health status."""
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
                self._instance,
                self._username,
                self._password,
            )
            results: list[dict[str, Any]] = data.get("result", [])
            instance_info = f"ServiceNow instance '{self._instance}' reachable"
            if results:
                user_name_hc: str = (
                    results[0].get("name", "")
                    or results[0].get("user_name", "")
                    or ""
                )
                if user_name_hc:
                    instance_info += f". User: {user_name_hc}"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=instance_info,
            )
        except ServiceNowAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ServiceNowNetworkError as exc:
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
        full: bool = False,  # noqa: ARG002  — reserved for incremental future
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync all ServiceNow incidents and change requests into the knowledge base."""
        found = 0
        synced = 0
        failed = 0

        # Sync incidents
        incident_result = await self._sync_table(table="incidents", kb_id=kb_id)
        found += incident_result[0]
        synced += incident_result[1]
        failed += incident_result[2]
        if incident_result[3]:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=incident_result[3],
            )

        # Sync change requests
        change_result = await self._sync_table(table="changes", kb_id=kb_id)
        found += change_result[0]
        synced += change_result[1]
        failed += change_result[2]
        if change_result[3]:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=change_result[3],
            )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _sync_table(
        self,
        table: str,
        kb_id: str,
    ) -> tuple[int, int, int, str]:
        """Paginate through a table and return (found, synced, failed, error_message)."""
        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0
        offset = 0

        while True:
            try:
                if table == "incidents":
                    page_data = await with_retry(
                        client.list_incidents,
                        self._instance,
                        self._username,
                        self._password,
                        limit=SYNC_PAGE_SIZE,
                        offset=offset,
                    )
                else:
                    page_data = await with_retry(
                        client.list_changes,
                        self._instance,
                        self._username,
                        self._password,
                        limit=SYNC_PAGE_SIZE,
                        offset=offset,
                    )
            except ServiceNowError as exc:
                return found, synced, failed, str(exc)

            records: list[dict[str, Any]] = page_data.get("result", [])
            found += len(records)

            for record in records:
                try:
                    if table == "incidents":
                        doc = normalize_incident(
                            record,
                            self.connector_id,
                            self.tenant_id,
                            self._instance,
                        )
                    else:
                        doc = normalize_change(
                            record,
                            self.connector_id,
                            self.tenant_id,
                            self._instance,
                        )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # ServiceNow uses offset-based pagination — stop when fewer records than limit
            if len(records) < SYNC_PAGE_SIZE:
                break
            offset += SYNC_PAGE_SIZE

        return found, synced, failed, ""

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Incidents ─────────────────────────────────────────────────────────────

    async def list_incidents(
        self,
        limit: int = 100,
        offset: int = 0,
        query: str | None = None,
        sysparm_query: str | None = None,
    ) -> dict[str, Any]:
        """Return a paginated list of ServiceNow incidents.

        Accepts either `query` or `sysparm_query` (alias) for the ServiceNow
        encoded query string (sysparm_query parameter).
        """
        client = self._ensure_client()
        effective_query = query or sysparm_query
        return await with_retry(
            client.list_incidents,
            self._instance,
            self._username,
            self._password,
            limit=limit,
            offset=offset,
            query=effective_query,
        )

    async def get_incident(self, sys_id: str) -> dict[str, Any]:
        """Return a single incident by sys_id."""
        client = self._ensure_client()
        return await with_retry(
            client.get_incident,
            self._instance,
            self._username,
            self._password,
            sys_id,
        )

    # ── Problems ──────────────────────────────────────────────────────────────

    async def list_problems(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return a paginated list of ServiceNow problem records."""
        client = self._ensure_client()
        return await with_retry(
            client.list_problems,
            self._instance,
            self._username,
            self._password,
            limit=limit,
            offset=offset,
        )

    # ── Change requests ───────────────────────────────────────────────────────

    async def list_changes(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return a paginated list of ServiceNow change requests."""
        client = self._ensure_client()
        return await with_retry(
            client.list_changes,
            self._instance,
            self._username,
            self._password,
            limit=limit,
            offset=offset,
        )

    async def get_change(self, sys_id: str) -> dict[str, Any]:
        """Return a single change request by sys_id."""
        client = self._ensure_client()
        return await with_retry(
            client.get_change,
            self._instance,
            self._username,
            self._password,
            sys_id,
        )

    # ── Service Catalog ───────────────────────────────────────────────────────

    async def list_service_catalog_items(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return a paginated list of ServiceNow service catalog items."""
        client = self._ensure_client()
        return await with_retry(
            client.list_service_catalog_items,
            self._instance,
            self._username,
            self._password,
            limit=limit,
            offset=offset,
        )

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return a paginated list of ServiceNow users."""
        client = self._ensure_client()
        return await with_retry(
            client.list_users,
            self._instance,
            self._username,
            self._password,
            limit=limit,
            offset=offset,
        )

    # ── CMDB ──────────────────────────────────────────────────────────────────

    async def list_cmdb_items(
        self,
        class_name: str = "cmdb_ci",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return CMDB configuration items for the given CI class."""
        client = self._ensure_client()
        return await with_retry(
            client.list_cmdb_items,
            self._instance,
            self._username,
            self._password,
            class_name=class_name,
            limit=limit,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> ServiceNowConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
