"""ClickUp connector — orchestration layer.

All HTTP calls    → client/http_client.py  (ClickUpHTTPClient)
Normalization     → helpers/utils.py
Models            → models.py
Exceptions        → exceptions.py

Auth type: Personal API Token (api_key).
Header:    Authorization: {api_key}   (raw — no "Bearer" prefix).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from client.http_client import ClickUpHTTPClient
from exceptions import ClickUpAuthError, ClickUpError, ClickUpNetworkError
from helpers.utils import normalize_list, normalize_task, with_retry
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

CONNECTOR_TYPE: str = "clickup"
AUTH_TYPE: str = "api_key"

_PAGE_SIZE: int = 100


class ClickUpConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for ClickUp via the ClickUp API v2.

    Authenticates using a Personal API Token (api_key) passed in the
    ``Authorization`` header as the raw token value (no "Bearer" prefix).

    Sync hierarchy: Teams → Spaces → Folders → Lists → Tasks (paginated).
    """

    CONNECTOR_TYPE: str = "clickup"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        _config: Dict[str, Any] = config or {}
        super().__init__(
            tenant_id=tenant_id,
            connector_id=connector_id,
            config=_config,
        )
        self._api_key: str = _config.get("api_key", "").strip()
        self._http_client: ClickUpHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> ClickUpHTTPClient:
        return ClickUpHTTPClient(api_key=self._api_key)

    def _ensure_client(self) -> ClickUpHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not self._api_key

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key is present and valid by calling GET /user."""
        if self._missing_creds():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: api_key",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_authorized_user)
            await client.aclose()
            user = data.get("user", data) if isinstance(data, dict) else {}
            username: str = (
                user.get("username", "")
                or user.get("email", "")
                or "Unknown user"
            )
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to ClickUp as {username}",
            )
        except ClickUpAuthError as exc:
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
        """Ping GET /user and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_authorized_user)
            await client.aclose()
            user = data.get("user", data) if isinstance(data, dict) else {}
            username: str = user.get("username", "") or user.get("email", "") or "unknown"
            email: str = user.get("email", "")
            msg = f"ClickUp API is reachable (user: {username}"
            if email and email != username:
                msg += f", {email}"
            msg += ")"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
            )
        except ClickUpAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ClickUpNetworkError as exc:
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
        since: Any = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync ClickUp: teams → spaces → lists → tasks.

        Walks the hierarchy depth-first, paginating tasks with page-based
        pagination. Failures in individual resources are isolated — the sync
        continues and records the failure count.
        """
        if self._http_client is None:
            self._http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # 1. Teams
        try:
            teams_data = await with_retry(self._http_client.get_teams)
        except ClickUpError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to list teams: {exc}",
            )

        teams: List[Dict[str, Any]] = teams_data.get("teams", [])

        for team in teams:
            team_id: str = str(team.get("id", "")) or ""
            if not team_id:
                continue

            # 2. Spaces
            try:
                spaces_data = await with_retry(self._http_client.get_spaces, team_id)
                spaces: List[Dict[str, Any]] = spaces_data.get("spaces", [])
            except ClickUpError:
                continue

            for space in spaces:
                space_id: str = str(space.get("id", "")) or ""
                if not space_id:
                    continue

                # 3. Folders → Lists → Tasks
                try:
                    folders_data = await with_retry(
                        self._http_client.get_folders, space_id
                    )
                    folders: List[Dict[str, Any]] = folders_data.get("folders", [])
                except ClickUpError:
                    folders = []

                for folder in folders:
                    folder_id: str = str(folder.get("id", "")) or ""
                    if not folder_id:
                        continue
                    try:
                        lists_data = await with_retry(
                            self._http_client.get_lists, folder_id=folder_id
                        )
                        task_lists: List[Dict[str, Any]] = lists_data.get("lists", [])
                    except ClickUpError:
                        continue

                    for task_list in task_lists:
                        found += 1
                        try:
                            list_doc = normalize_list(task_list, space_id=space_id)
                            if kb_id:
                                await self._ingest_document(list_doc, kb_id)
                            synced += 1
                        except Exception:
                            failed += 1

                        list_id: str = str(task_list.get("id", "")) or ""
                        if list_id:
                            f, s, x = await self._sync_tasks(list_id, space_id, kb_id)
                            found += f
                            synced += s
                            failed += x

                # Folderless lists in this space
                try:
                    fl_data = await with_retry(
                        self._http_client.get_lists, space_id=space_id
                    )
                    fl_lists: List[Dict[str, Any]] = fl_data.get("lists", [])
                except ClickUpError:
                    fl_lists = []

                for fl_list in fl_lists:
                    found += 1
                    try:
                        fl_doc = normalize_list(fl_list, space_id=space_id)
                        if kb_id:
                            await self._ingest_document(fl_doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                    fl_id: str = str(fl_list.get("id", "")) or ""
                    if fl_id:
                        f, s, x = await self._sync_tasks(fl_id, space_id, kb_id)
                        found += f
                        synced += s
                        failed += x

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _sync_tasks(
        self,
        list_id: str,
        space_id: str,
        kb_id: str,
    ) -> tuple[int, int, int]:
        """Paginate through all tasks in a list and normalize them.

        Uses page-based pagination: stops when the page returns fewer tasks
        than the page size or returns an empty tasks list.
        Returns (found, synced, failed).
        """
        found = 0
        synced = 0
        failed = 0
        page = 0

        while True:
            try:
                tasks_data = await with_retry(
                    self._http_client.get_tasks,  # type: ignore[union-attr]
                    list_id,
                    page=page,
                )
                tasks: List[Dict[str, Any]] = tasks_data.get("tasks", [])
            except ClickUpError:
                break

            if not tasks:
                break

            found += len(tasks)
            for task in tasks:
                try:
                    doc = normalize_task(task, list_id=list_id, space_id=space_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # Stop if fewer items than page size (last page)
            if len(tasks) < _PAGE_SIZE or tasks_data.get("last_page"):
                break
            page += 1

        return found, synced, failed

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── List methods ──────────────────────────────────────────────────────────

    async def list_teams(self) -> List[Dict[str, Any]]:
        """Return all authorized workspaces (teams)."""
        client = self._ensure_client()
        data = await with_retry(client.get_teams)
        return data.get("teams", [])

    async def list_spaces(self, team_id: str) -> List[Dict[str, Any]]:
        """Return all spaces in a workspace."""
        client = self._ensure_client()
        data = await with_retry(client.get_spaces, team_id)
        return data.get("spaces", [])

    async def list_folders(self, space_id: str) -> List[Dict[str, Any]]:
        """Return all folders in a space."""
        client = self._ensure_client()
        data = await with_retry(client.get_folders, space_id)
        return data.get("folders", [])

    async def list_lists(
        self,
        space_id: Optional[str] = None,
        folder_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return all lists in a space or folder."""
        client = self._ensure_client()
        data = await with_retry(client.get_lists, space_id=space_id, folder_id=folder_id)
        return data.get("lists", [])

    async def list_tasks(
        self,
        list_id: str,
        include_closed: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return all tasks in a list, auto-paginating until empty."""
        client = self._ensure_client()
        all_tasks: List[Dict[str, Any]] = []
        page = 0

        while True:
            data = await with_retry(
                client.get_tasks, list_id, page=page, include_closed=include_closed
            )
            tasks: List[Dict[str, Any]] = data.get("tasks", [])
            if not tasks:
                break
            all_tasks.extend(tasks)
            if len(tasks) < _PAGE_SIZE or data.get("last_page"):
                break
            page += 1

        return all_tasks

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        """Return a single task by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_task, task_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> ClickUpConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
