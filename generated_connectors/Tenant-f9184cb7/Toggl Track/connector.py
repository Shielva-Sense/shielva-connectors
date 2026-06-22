"""Toggl Track connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Toggl Track REST API v9 uses HTTP Basic auth where the api_token
is the username and the LITERAL string "api_token" is the password:

    Authorization: Basic base64("<api_token>:api_token")

This is a Toggl-specific quirk — not a bearer token. Sending the token
as `Bearer <token>` returns 403.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import TogglHTTPClient
from exceptions import (
    TogglAuthError,
    TogglError,
    TogglNetworkError,
    TogglNotFound,
)
from helpers.normalizer import normalize_project, normalize_time_entry
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_TOGGL_BASE = "https://api.track.toggl.com/api/v9"


class TogglConnector(BaseConnector):
    """Shielva connector for the Toggl Track REST API v9.

    Surfaces: Me, Workspaces, Projects, Time Entries, Tags, Clients, Tasks.
    """

    CONNECTOR_TYPE = "toggl"
    CONNECTOR_NAME = "Toggl Track"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_token",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_token: str = self.config.get("api_token", "")
        self.default_workspace_id: Any = self.config.get("default_workspace_id", "")
        self.base_url: str = self.config.get("base_url", "") or _TOGGL_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = TogglHTTPClient(
            api_token=self.api_token,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Toggl API-key install only requires `api_token`. The
        `default_workspace_id` is optional — methods that omit
        `workspace_id` fall back to this value (or to `me.default_workspace_id`).
        """
        api_token = self.config.get("api_token")

        if not api_token:
            logger.warning(
                "toggl.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )

        await self.save_config(
            {
                "api_token": api_token,
                "default_workspace_id": self.config.get("default_workspace_id", ""),
                "base_url": self.config.get("base_url", _TOGGL_BASE),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 60),
            }
        )
        logger.info("toggl.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Toggl Track connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_token.
        """
        return TokenInfo(
            access_token=self.api_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Toggl API connectivity by calling GET /me."""
        try:
            await with_retry(
                lambda: self.http_client.get_me(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Toggl Track API reachable",
            )
        except TogglAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Toggl auth failed: {exc}",
            )
        except TogglNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Toggl network error: {exc}",
            )
        except TogglError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Sync Toggl Track projects + time entries into the Shielva KB.

        For each workspace (the default workspace, or every workspace returned
        by /workspaces when default_workspace_id is blank), page through
        projects and ingest them; then pull recent time entries from
        /me/time_entries.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Resolve workspaces to walk.
            if self.default_workspace_id:
                workspace_ids = [self.default_workspace_id]
            else:
                workspaces_resp = await self.http_client.list_workspaces()
                if isinstance(workspaces_resp, list):
                    workspace_ids = [
                        w.get("id") for w in workspaces_resp if isinstance(w, dict) and w.get("id")
                    ]
                else:
                    workspace_ids = []

            for wid in workspace_ids:
                try:
                    projects_resp = await with_retry(
                        lambda w=wid: self.http_client.list_projects(w),
                        max_retries=3,
                    )
                except TogglError as exc:
                    logger.warning("toggl.sync.projects_failed", workspace_id=wid, error=str(exc))
                    projects_resp = []

                projects_list = projects_resp if isinstance(projects_resp, list) else []
                for raw in projects_list:
                    documents_found += 1
                    try:
                        doc = normalize_project(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("toggl.sync.project_failed", error=str(exc))
                        documents_failed += 1

            # Time entries (scoped to the api_token's user via /me)
            try:
                entries_resp = await with_retry(
                    lambda: self.http_client.list_time_entries(),
                    max_retries=3,
                )
            except TogglError as exc:
                logger.warning("toggl.sync.time_entries_failed", error=str(exc))
                entries_resp = []

            entries_list = entries_resp if isinstance(entries_resp, list) else []
            for raw in entries_list:
                documents_found += 1
                try:
                    doc = normalize_time_entry(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error("toggl.sync.time_entry_failed", error=str(exc))
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Toggl documents",
            )
        except Exception as exc:
            logger.error("toggl.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    # Me ------------------------------------------------------------------
    async def get_me(self) -> Dict[str, Any]:
        """GET /me — authenticated user profile."""
        return await with_retry(
            lambda: self.http_client.get_me(),
            max_retries=3,
        )

    # Workspaces ----------------------------------------------------------
    async def list_workspaces(self) -> Any:
        """GET /workspaces — workspaces the user belongs to."""
        return await with_retry(
            lambda: self.http_client.list_workspaces(),
            max_retries=3,
        )

    async def get_workspace(self, workspace_id: Any) -> Dict[str, Any]:
        """GET /workspaces/{wid}."""
        return await with_retry(
            lambda: self.http_client.get_workspace(workspace_id),
            max_retries=3,
        )

    # Projects ------------------------------------------------------------
    async def list_projects(
        self,
        workspace_id: Any,
        active: Optional[bool] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
    ) -> Any:
        """GET /workspaces/{wid}/projects."""
        return await with_retry(
            lambda: self.http_client.list_projects(
                workspace_id,
                active=active,
                page=page,
                per_page=per_page,
            ),
            max_retries=3,
        )

    async def get_project(self, workspace_id: Any, project_id: Any) -> Dict[str, Any]:
        """GET /workspaces/{wid}/projects/{pid}."""
        return await with_retry(
            lambda: self.http_client.get_project(workspace_id, project_id),
            max_retries=3,
        )

    async def create_project(
        self,
        workspace_id: Any,
        project: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /workspaces/{wid}/projects."""
        return await self.http_client.create_project(workspace_id, project)

    # Time entries --------------------------------------------------------
    async def list_time_entries(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        since: Optional[int] = None,
    ) -> Any:
        """GET /me/time_entries."""
        return await with_retry(
            lambda: self.http_client.list_time_entries(
                start_date=start_date,
                end_date=end_date,
                since=since,
            ),
            max_retries=3,
        )

    async def get_current_time_entry(self) -> Any:
        """GET /me/time_entries/current — running entry or null."""
        return await with_retry(
            lambda: self.http_client.get_current_time_entry(),
            max_retries=3,
        )

    async def create_time_entry(
        self,
        workspace_id: Any,
        entry: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /workspaces/{wid}/time_entries."""
        return await self.http_client.create_time_entry(workspace_id, entry)

    async def update_time_entry(
        self,
        workspace_id: Any,
        time_entry_id: Any,
        entry: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /workspaces/{wid}/time_entries/{teid}."""
        return await self.http_client.update_time_entry(workspace_id, time_entry_id, entry)

    async def stop_time_entry(
        self,
        workspace_id: Any,
        time_entry_id: Any,
    ) -> Dict[str, Any]:
        """PATCH /workspaces/{wid}/time_entries/{teid}/stop."""
        return await self.http_client.stop_time_entry(workspace_id, time_entry_id)

    async def delete_time_entry(
        self,
        workspace_id: Any,
        time_entry_id: Any,
    ) -> Dict[str, Any]:
        """DELETE /workspaces/{wid}/time_entries/{teid}."""
        return await self.http_client.delete_time_entry(workspace_id, time_entry_id)

    # Tags / Clients / Tasks ---------------------------------------------
    async def list_tags(self, workspace_id: Any) -> Any:
        """GET /workspaces/{wid}/tags."""
        return await with_retry(
            lambda: self.http_client.list_tags(workspace_id),
            max_retries=3,
        )

    async def list_clients(self, workspace_id: Any) -> Any:
        """GET /workspaces/{wid}/clients."""
        return await with_retry(
            lambda: self.http_client.list_clients(workspace_id),
            max_retries=3,
        )

    async def create_client(
        self,
        workspace_id: Any,
        client: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /workspaces/{wid}/clients."""
        return await self.http_client.create_client(workspace_id, client)

    async def list_tasks(
        self,
        workspace_id: Any,
        project_id: Any,
    ) -> Any:
        """GET /workspaces/{wid}/projects/{pid}/tasks."""
        return await with_retry(
            lambda: self.http_client.list_tasks(workspace_id, project_id),
            max_retries=3,
        )
