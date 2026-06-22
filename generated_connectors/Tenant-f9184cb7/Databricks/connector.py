from __future__ import annotations

from typing import Any

from client.http_client import DatabricksHTTPClient
from exceptions import DatabricksAuthError, DatabricksError, DatabricksNetworkError
from helpers.utils import (
    normalize_cluster,
    normalize_job,
    normalize_model,
    normalize_notebook,
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

CONNECTOR_TYPE = "databricks"
AUTH_TYPE = "api_key"

# Maximum pages to pull during a full sync (safety cap)
_MAX_JOB_PAGES: int = 40
_MAX_NOTEBOOK_DEPTH: int = 3


class DatabricksConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Databricks unified analytics platform.

    Provides authentication, health checks, full sync, and direct API access
    for clusters, jobs, notebooks, MLflow experiments, registered models,
    and SQL warehouses.

    Auth: Personal Access Token sent as Authorization: Bearer {token}.
    Base URL: workspace_url (e.g. https://adb-123456.azuredatabricks.net).
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
        if type(BaseConnector) is not type(object):
            try:
                super().__init__(
                    tenant_id=tenant_id, connector_id=connector_id, config=_config
                )
            except TypeError:
                self.tenant_id = tenant_id
                self.connector_id = connector_id
                self.config = _config
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = _config

        self._token: str = _config.get("token", "")
        self._workspace_url: str = _config.get("workspace_url", "").rstrip("/")
        self.client: DatabricksHTTPClient = DatabricksHTTPClient(config=self.config)

    def _make_client(self) -> DatabricksHTTPClient:
        return DatabricksHTTPClient(config=self.config)

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate workspace_url and token via GET /api/2.0/preview/scim/v2/Me."""
        if not self._workspace_url:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="workspace_url is required",
            )
        if not self._token:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="token is required",
            )
        client = self._make_client()
        try:
            user = await with_retry(client.get_current_user)
            await client.aclose()
            self.client = self._make_client()
            email: str = user.get("userName", user.get("emails", [{}])[0].get("value", ""))
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Databricks workspace ({email})",
            )
        except DatabricksAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Databricks credentials: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /api/2.0/preview/scim/v2/Me and return current health."""
        if not self._workspace_url or not self._token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="workspace_url and token are required",
            )
        client = self._make_client()
        try:
            user = await with_retry(client.get_current_user)
            await client.aclose()
            email: str = user.get("userName", user.get("emails", [{}])[0].get("value", ""))
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Databricks workspace ({email})",
                email=email,
            )
        except DatabricksAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except DatabricksNetworkError as exc:
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
        full: bool = False,  # noqa: ARG002
        since: Any = None,  # noqa: ARG002
        kb_id: str = "",
    ) -> SyncResult:
        """Sync clusters, jobs, and notebooks from Databricks."""
        found = 0
        synced = 0
        failed = 0

        # Sync clusters
        try:
            clusters = await self.list_clusters()
            found += len(clusters)
            for raw in clusters:
                try:
                    doc = normalize_cluster(
                        raw,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except DatabricksError:
            pass

        # Sync jobs
        try:
            jobs = await self.list_jobs()
            found += len(jobs)
            for raw in jobs:
                try:
                    doc = normalize_job(
                        raw,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except DatabricksError:
            pass

        # Sync notebooks
        try:
            notebooks = await self.list_notebooks()
            found += len(notebooks)
            for raw in notebooks:
                try:
                    doc = normalize_notebook(
                        raw,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except DatabricksError:
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        if found == 0 and synced == 0:
            status = SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Clusters ──────────────────────────────────────────────────────────────

    async def list_clusters(self) -> list[dict[str, Any]]:
        """Fetch all clusters in the workspace."""
        result = await with_retry(self.client.list_clusters)
        return result.get("clusters", [])

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def list_jobs(self, limit: int = 25) -> list[dict[str, Any]]:
        """Fetch all jobs from the workspace, paginating automatically."""
        all_jobs: list[dict[str, Any]] = []
        for page in range(_MAX_JOB_PAGES):
            offset = page * limit
            result = await with_retry(self.client.list_jobs, limit=limit, offset=offset)
            batch: list[dict[str, Any]] = result.get("jobs", [])
            if not batch:
                break
            all_jobs.extend(batch)
            if not result.get("has_more", False):
                break
        return all_jobs

    # ── Notebooks ─────────────────────────────────────────────────────────────

    async def list_notebooks(self, path: str = "/") -> list[dict[str, Any]]:
        """Fetch workspace objects (notebooks/directories) at the given path."""
        result = await with_retry(self.client.list_notebooks, path=path)
        objects: list[dict[str, Any]] = result.get("objects", [])
        # Return only NOTEBOOK type objects (not DIRECTORY)
        return [o for o in objects if o.get("object_type") == "NOTEBOOK"]

    # ── MLflow experiments ────────────────────────────────────────────────────

    async def list_experiments(self) -> list[dict[str, Any]]:
        """Fetch all MLflow experiments in the workspace."""
        result = await with_retry(self.client.list_experiments)
        return result.get("experiments", [])

    # ── MLflow models ─────────────────────────────────────────────────────────

    async def list_models(self) -> list[dict[str, Any]]:
        """Fetch all MLflow registered models in the workspace."""
        result = await with_retry(self.client.list_models)
        return result.get("registered_models", [])

    # ── SQL Warehouses ────────────────────────────────────────────────────────

    async def list_sql_warehouses(self) -> list[dict[str, Any]]:
        """Fetch all SQL warehouses in the workspace."""
        result = await with_retry(self.client.list_sql_warehouses)
        return result.get("warehouses", [])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def __aenter__(self) -> DatabricksConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
