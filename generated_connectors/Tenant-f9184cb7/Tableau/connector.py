from __future__ import annotations

from typing import Any

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

from client import TableauHTTPClient
from exceptions import TableauAuthError, TableauError, TableauNetworkError
from helpers import (
    CircuitBreaker,
    normalize_datasource,
    normalize_view,
    normalize_workbook,
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

SYNC_PAGE_SIZE = 100
CIRCUIT_BREAKER_THRESHOLD = 5


class TableauConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Tableau Server / Tableau Cloud.

    Authenticates via Personal Access Token (PAT), health-checks the REST API,
    and syncs Workbooks, Views, and Datasources into the Shielva knowledge base.
    """

    CONNECTOR_TYPE: str = "tableau"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if BaseConnector is not object:
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

        self._server_url: str = _config.get("server_url", "").rstrip("/")
        self._pat_name: str = _config.get("pat_name", "")
        self._pat_secret: str = _config.get("pat_secret", "")
        self._site_name: str = _config.get("site_name", "")
        self.http_client: TableauHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> TableauHTTPClient:
        return TableauHTTPClient(
            server_url=self._server_url,
            pat_name=self._pat_name,
            pat_secret=self._pat_secret,
            site_name=self._site_name,
        )

    def _ensure_client(self) -> TableauHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that server_url, pat_name, and pat_secret are present."""
        missing = [
            f for f in ("server_url", "pat_name", "pat_secret")
            if not self.config.get(f)
        ]
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            await with_retry(client.sign_in)
            await client.sign_out()
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Tableau at {self._server_url}",
            )
        except TableauAuthError as exc:
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
        """Sign in, retrieve current site info, and return health status."""
        if not self._server_url or not self._pat_name or not self._pat_secret:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="server_url, pat_name, and pat_secret are required",
            )

        client = self._make_client()
        try:
            await with_retry(client.sign_in)
            site_id = client.site_id
            await client.sign_out()
            await client.aclose()
            self._circuit_breaker.on_success()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Tableau API is reachable. Site ID: {site_id}",
            )
        except TableauAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except TableauNetworkError as exc:
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

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sign in then fetch workbooks, views, and datasources; normalize each to SyncResult."""
        client = self._make_client()
        found = 0
        synced = 0
        failed = 0

        try:
            await with_retry(client.sign_in)
        except TableauAuthError as exc:
            await client.aclose()
            return SyncResult(
                status=SyncStatus.FAILED,
                message=str(exc),
            )
        except TableauError as exc:
            await client.aclose()
            return SyncResult(
                status=SyncStatus.FAILED,
                message=str(exc),
            )

        site_id = client.site_id
        kb_id: str = kwargs.get("kb_id", "")

        # ── Workbooks ─────────────────────────────────────────────────────────
        page = 1
        while True:
            try:
                data = await with_retry(client.get_workbooks, site_id, page, SYNC_PAGE_SIZE)
            except TableauError:
                failed += 1
                break

            workbooks: list[dict[str, Any]] = (
                data.get("workbooks", {}).get("workbook", [])
                if isinstance(data.get("workbooks"), dict)
                else []
            )
            found += len(workbooks)
            for wb in workbooks:
                try:
                    doc = normalize_workbook(wb, self.connector_id, self.tenant_id, self._server_url)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            pagination = data.get("workbooks", {}).get("pagination", {}) if isinstance(data.get("workbooks"), dict) else {}
            total = int(pagination.get("totalAvailable", 0))
            if not workbooks or synced + failed >= total or total == 0:
                break
            page += 1

        # ── Views ─────────────────────────────────────────────────────────────
        page = 1
        while True:
            try:
                data = await with_retry(client.get_views, site_id, page, SYNC_PAGE_SIZE)
            except TableauError:
                failed += 1
                break

            views: list[dict[str, Any]] = (
                data.get("views", {}).get("view", [])
                if isinstance(data.get("views"), dict)
                else []
            )
            found += len(views)
            for v in views:
                try:
                    doc = normalize_view(v, self.connector_id, self.tenant_id, self._server_url)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            pagination = data.get("views", {}).get("pagination", {}) if isinstance(data.get("views"), dict) else {}
            total = int(pagination.get("totalAvailable", 0))
            if not views or synced + failed >= total or total == 0:
                break
            page += 1

        # ── Datasources ───────────────────────────────────────────────────────
        page = 1
        while True:
            try:
                data = await with_retry(client.get_datasources, site_id, page, SYNC_PAGE_SIZE)
            except TableauError:
                failed += 1
                break

            datasources: list[dict[str, Any]] = (
                data.get("datasources", {}).get("datasource", [])
                if isinstance(data.get("datasources"), dict)
                else []
            )
            found += len(datasources)
            for ds in datasources:
                try:
                    doc = normalize_datasource(ds, self.connector_id, self.tenant_id, self._server_url)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            pagination = data.get("datasources", {}).get("pagination", {}) if isinstance(data.get("datasources"), dict) else {}
            total = int(pagination.get("totalAvailable", 0))
            if not datasources or synced + failed >= total or total == 0:
                break
            page += 1

        try:
            await client.sign_out()
        except Exception:
            pass
        await client.aclose()

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Public list methods ───────────────────────────────────────────────────

    async def list_workbooks(self) -> list[dict[str, Any]]:
        """Return all workbooks for the current site (auto-paginated)."""
        client = self._make_client()
        await with_retry(client.sign_in)
        site_id = client.site_id
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await with_retry(client.get_workbooks, site_id, page, SYNC_PAGE_SIZE)
            items: list[dict[str, Any]] = (
                data.get("workbooks", {}).get("workbook", [])
                if isinstance(data.get("workbooks"), dict)
                else []
            )
            results.extend(items)
            pagination = data.get("workbooks", {}).get("pagination", {}) if isinstance(data.get("workbooks"), dict) else {}
            total = int(pagination.get("totalAvailable", 0))
            if not items or len(results) >= total or total == 0:
                break
            page += 1
        await client.sign_out()
        await client.aclose()
        return results

    async def list_views(self) -> list[dict[str, Any]]:
        """Return all views for the current site (auto-paginated)."""
        client = self._make_client()
        await with_retry(client.sign_in)
        site_id = client.site_id
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await with_retry(client.get_views, site_id, page, SYNC_PAGE_SIZE)
            items: list[dict[str, Any]] = (
                data.get("views", {}).get("view", [])
                if isinstance(data.get("views"), dict)
                else []
            )
            results.extend(items)
            pagination = data.get("views", {}).get("pagination", {}) if isinstance(data.get("views"), dict) else {}
            total = int(pagination.get("totalAvailable", 0))
            if not items or len(results) >= total or total == 0:
                break
            page += 1
        await client.sign_out()
        await client.aclose()
        return results

    async def list_datasources(self) -> list[dict[str, Any]]:
        """Return all datasources for the current site (auto-paginated)."""
        client = self._make_client()
        await with_retry(client.sign_in)
        site_id = client.site_id
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await with_retry(client.get_datasources, site_id, page, SYNC_PAGE_SIZE)
            items: list[dict[str, Any]] = (
                data.get("datasources", {}).get("datasource", [])
                if isinstance(data.get("datasources"), dict)
                else []
            )
            results.extend(items)
            pagination = data.get("datasources", {}).get("pagination", {}) if isinstance(data.get("datasources"), dict) else {}
            total = int(pagination.get("totalAvailable", 0))
            if not items or len(results) >= total or total == 0:
                break
            page += 1
        await client.sign_out()
        await client.aclose()
        return results

    async def list_users(self) -> list[dict[str, Any]]:
        """Return all users for the current site (auto-paginated)."""
        client = self._make_client()
        await with_retry(client.sign_in)
        site_id = client.site_id
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await with_retry(client.get_users, site_id, page, SYNC_PAGE_SIZE)
            items: list[dict[str, Any]] = (
                data.get("users", {}).get("user", [])
                if isinstance(data.get("users"), dict)
                else []
            )
            results.extend(items)
            pagination = data.get("users", {}).get("pagination", {}) if isinstance(data.get("users"), dict) else {}
            total = int(pagination.get("totalAvailable", 0))
            if not items or len(results) >= total or total == 0:
                break
            page += 1
        await client.sign_out()
        await client.aclose()
        return results

    async def list_projects(self) -> list[dict[str, Any]]:
        """Return all projects for the current site."""
        client = self._make_client()
        await with_retry(client.sign_in)
        site_id = client.site_id
        data = await with_retry(client.get_projects, site_id)
        projects: list[dict[str, Any]] = (
            data.get("projects", {}).get("project", [])
            if isinstance(data.get("projects"), dict)
            else []
        )
        await client.sign_out()
        await client.aclose()
        return projects

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> TableauConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
