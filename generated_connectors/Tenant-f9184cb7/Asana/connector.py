from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import AsanaHTTPClient
from exceptions import AsanaAuthError, AsanaError, AsanaNetworkError
from helpers import normalize_project, normalize_task, with_retry
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
        def __init__(self, tenant_id: str = "", connector_id: str = "", config: Dict[str, Any] | None = None) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

CONNECTOR_TYPE: str = "asana"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 100


class AsanaConnector(BaseConnector):
    """
    Shielva connector for Asana.

    Syncs tasks and projects from all workspaces in an Asana account using a
    Personal Access Token (Bearer token) for authentication.

    Auth field: ``api_key`` — Asana Personal Access Token.
    Optional field: ``workspace_gid`` — Asana Workspace/Organization GID.
    """

    CONNECTOR_TYPE: str = "asana"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)

        self._api_key: str = _config.get("api_key", "").strip()
        self._workspace_gid: str = _config.get("workspace_gid", "").strip()
        self._http_client: AsanaHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> AsanaHTTPClient:
        return AsanaHTTPClient()

    def _ensure_client(self) -> AsanaHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not self._api_key

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key by calling GET /users/me."""
        if self._missing_creds():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: api_key",
            )

        client = self._make_client()
        try:
            user = await with_retry(client.get_current_user, self._api_key)
            await client.aclose()
            user_name: str = (
                user.get("name", "")
                or user.get("email", "")
                or "Unknown user"
            )
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Asana as {user_name}",
            )
        except AsanaAuthError as exc:
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
        """Ping GET /users/me and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        client = self._make_client()
        try:
            user = await with_retry(client.get_current_user, self._api_key)
            await client.aclose()
            user_name: str = user.get("name", "") or user.get("email", "") or "unknown"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Asana API is reachable (user: {user_name})",
            )
        except AsanaAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except AsanaNetworkError as exc:
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

    async def sync(self, **kwargs: Any) -> SyncResult:
        """
        Sync tasks and projects from all workspaces in Asana.

        Iterates workspaces → projects → tasks with cursor-based pagination
        using next_page.offset. When workspace_gid is configured, only that
        workspace is synced.
        """
        if self._http_client is None:
            self._http_client = self._make_client()

        kb_id: str = kwargs.get("kb_id", "")
        found = 0
        synced = 0
        failed = 0

        # Determine workspaces to sync
        try:
            ws_resp = await with_retry(self._http_client.list_workspaces, self._api_key)
            workspaces: list[dict[str, Any]] = ws_resp.get("data", [])
        except AsanaError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to list workspaces: {exc}",
            )

        # Filter to configured workspace if provided
        if self._workspace_gid:
            workspaces = [w for w in workspaces if w.get("gid") == self._workspace_gid]

        for workspace in workspaces:
            workspace_gid: str = workspace.get("gid", "") or ""
            if not workspace_gid:
                continue

            # Paginate through projects in this workspace
            project_offset: str | None = None
            while True:
                try:
                    projects_resp = await with_retry(
                        self._http_client.list_projects,
                        self._api_key,
                        workspace_gid,
                        False,
                        project_offset,
                    )
                except AsanaError:
                    break

                projects: list[dict[str, Any]] = projects_resp.get("data", [])
                if not projects:
                    break
                found += len(projects)

                for project in projects:
                    project_gid: str = project.get("gid", "") or ""
                    if not project_gid:
                        continue

                    # Ingest project as a document
                    try:
                        proj_doc = normalize_project(project, workspace_gid)
                        if kb_id:
                            await self._ingest_document(proj_doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                    # Paginate through tasks in this project
                    task_offset: str | None = None
                    while True:
                        try:
                            tasks_resp = await with_retry(
                                self._http_client.list_tasks,
                                self._api_key,
                                project_gid,
                                task_offset,
                            )
                        except AsanaError:
                            break

                        tasks: list[dict[str, Any]] = tasks_resp.get("data", [])
                        if not tasks:
                            break
                        found += len(tasks)

                        for task in tasks:
                            try:
                                doc = normalize_task(task, project_gid)
                                if kb_id:
                                    await self._ingest_document(doc, kb_id)
                                synced += 1
                            except Exception:
                                failed += 1

                        next_page = tasks_resp.get("next_page")
                        if next_page and next_page.get("offset"):
                            task_offset = next_page["offset"]
                        else:
                            break

                next_page_proj = projects_resp.get("next_page")
                if next_page_proj and next_page_proj.get("offset"):
                    project_offset = next_page_proj["offset"]
                else:
                    break

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

    # ── Workspace methods ─────────────────────────────────────────────────────

    async def list_workspaces(self) -> list[dict[str, Any]]:
        """Return all workspaces the user has access to."""
        client = self._ensure_client()
        resp = await with_retry(client.list_workspaces, self._api_key)
        return resp.get("data", [])

    # ── Project methods ───────────────────────────────────────────────────────

    async def list_projects(
        self,
        workspace_gid: str | None = None,
        archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Return list of project dicts from a workspace."""
        wgid = workspace_gid or self._workspace_gid
        client = self._ensure_client()
        resp = await with_retry(
            client.list_projects, self._api_key, wgid, archived
        )
        return resp.get("data", [])

    async def get_project(self, project_gid: str) -> dict[str, Any]:
        """Return a single project by GID."""
        client = self._ensure_client()
        return await with_retry(client.get_project, self._api_key, project_gid)

    # ── Task methods ──────────────────────────────────────────────────────────

    async def list_tasks(self, project_gid: str) -> list[dict[str, Any]]:
        """Return list of task dicts in a project."""
        client = self._ensure_client()
        resp = await with_retry(client.list_tasks, self._api_key, project_gid)
        return resp.get("data", [])

    async def get_task(self, task_gid: str) -> dict[str, Any]:
        """Return a single task by GID."""
        client = self._ensure_client()
        return await with_retry(client.get_task, self._api_key, task_gid)

    # ── Section methods ───────────────────────────────────────────────────────

    async def list_sections(self, project_gid: str) -> list[dict[str, Any]]:
        """Return all sections in a project."""
        client = self._ensure_client()
        return await with_retry(client.list_sections, self._api_key, project_gid)

    # ── User methods ──────────────────────────────────────────────────────────

    async def list_users(
        self, workspace_gid: str | None = None
    ) -> list[dict[str, Any]]:
        """Return list of user dicts in a workspace."""
        wgid = workspace_gid or self._workspace_gid
        client = self._ensure_client()
        resp = await with_retry(client.list_users, self._api_key, wgid)
        return resp.get("data", [])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> AsanaConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
