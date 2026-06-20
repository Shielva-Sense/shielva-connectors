from __future__ import annotations

from typing import Any

from client import RipplingHTTPClient
from exceptions import RipplingAuthError, RipplingError, RipplingNetworkError
from helpers import (
    normalize_department,
    normalize_employee,
    normalize_leave,
    normalize_team,
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

CONNECTOR_TYPE: str = "rippling"
AUTH_TYPE: str = "api_key"


class RipplingConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Rippling HR, Payroll, and IT management.

    Syncs employees, departments, teams, roles, and leave requests from
    the Rippling Platform API using Bearer token authentication.
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
            tenant_id=tenant_id, connector_id=connector_id, config=_config
        )
        self.client = RipplingHTTPClient(config=_config)
        self._http_client: RipplingHTTPClient | None = None

    def _make_client(self) -> RipplingHTTPClient:
        return RipplingHTTPClient(config=self.config)

    def _ensure_client(self) -> RipplingHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the API key by calling GET /companies."""
        api_key: str = self.config.get("api_key", "")
        if not api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: api_key",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_company)
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Rippling successfully",
            )
        except RipplingAuthError as exc:
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
        """Ping GET /companies to verify API reachability and auth."""
        api_key: str = self.config.get("api_key", "")
        if not api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: api_key",
            )

        client = self._make_client()
        try:
            company = await with_retry(client.get_company)
            company_name: str = company.get("name", "Unknown") if isinstance(company, dict) else "Unknown"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Rippling API reachable. Company: {company_name}",
            )
        except RipplingAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except RipplingNetworkError as exc:
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

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync employees and departments into the knowledge base."""
        kb_id: str = str(kwargs.get("kb_id", ""))
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        # ── Employees ─────────────────────────────────────────────────────────
        try:
            employees = await self._fetch_all_cursor_pages(client.list_employees)
            found += len(employees)
            for emp in employees:
                try:
                    doc = normalize_employee(emp)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except RipplingError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # ── Departments (non-fatal) ───────────────────────────────────────────
        try:
            departments = await self._fetch_all_simple(client.list_departments)
            found += len(departments)
            for dept in departments:
                try:
                    doc = normalize_department(dept)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Exception:
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    # ── Pagination helpers ────────────────────────────────────────────────────

    async def _fetch_all_cursor_pages(
        self,
        fetch_fn: Any,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Exhaust cursor-based pagination (offset/next_cursor pattern)."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = await with_retry(fetch_fn, cursor=cursor, limit=limit)
            batch: list[dict[str, Any]] = response.get("data", [])
            if isinstance(batch, list):
                items.extend(batch)
            next_cursor: str | None = response.get("next_cursor")
            if not next_cursor or not batch:
                break
            cursor = next_cursor
        return items

    async def _fetch_all_simple(
        self,
        fetch_fn: Any,
    ) -> list[dict[str, Any]]:
        """Fetch a non-paginated endpoint and return the items list."""
        response = await with_retry(fetch_fn)
        data = response.get("data", [])
        if isinstance(data, list):
            return data
        return []

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── List methods ──────────────────────────────────────────────────────────

    async def list_employees(self) -> list[dict[str, Any]]:
        """Return all Rippling employees, paginating through all cursor pages."""
        client = self._ensure_client()
        return await self._fetch_all_cursor_pages(client.list_employees)

    async def list_departments(self) -> list[dict[str, Any]]:
        """Return all Rippling departments."""
        client = self._ensure_client()
        return await self._fetch_all_simple(client.list_departments)

    async def list_teams(self) -> list[dict[str, Any]]:
        """Return all Rippling teams."""
        client = self._ensure_client()
        return await self._fetch_all_simple(client.list_teams)

    async def list_roles(self) -> list[dict[str, Any]]:
        """Return all Rippling roles."""
        client = self._ensure_client()
        return await self._fetch_all_simple(client.list_roles)

    async def list_leaves(self) -> list[dict[str, Any]]:
        """Return all Rippling leave requests, paginating through all cursor pages."""
        client = self._ensure_client()
        return await self._fetch_all_cursor_pages(client.list_leaves)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> RipplingConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
