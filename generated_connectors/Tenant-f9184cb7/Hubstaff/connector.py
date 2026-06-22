"""Hubstaff connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Personal Access Token (PAT) — treated as an opaque API key. Sent as
``Authorization: Bearer <access_token>`` on every request. There is no OAuth
exchange and no token refresh.
"""
from datetime import datetime, timedelta, timezone
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

from client.http_client import HubstaffHTTPClient
from exceptions import (
    HubstaffAuthError,
    HubstaffError,
    HubstaffNetworkError,
    HubstaffNotFound,
)
from helpers.normalizer import (
    normalize_activity,
    normalize_project,
    normalize_task,
)
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_HUBSTAFF_BASE = "https://api.hubstaff.com/v2"


class HubstaffConnector(BaseConnector):
    """Shielva connector for the Hubstaff v2 REST API (Organizations, Users, Teams, Projects, Tasks, Activities, Time Entries, Screenshots, Apps, URLs, Notes)."""

    CONNECTOR_TYPE = "hubstaff"
    CONNECTOR_NAME = "Hubstaff"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "access_token",
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
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.access_token: str = self.config.get("access_token", "")
        self.default_organization_id: Optional[int] = self._coerce_int(
            self.config.get("default_organization_id")
        )
        self.base_url: str = self.config.get("base_url", "") or _HUBSTAFF_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = HubstaffHTTPClient(
            access_token=self.access_token,
            base_url=self.base_url,
        )

    # ── Internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Hubstaff PAT install only requires `access_token`. The
        `default_organization_id` is optional and used by `sync()` and
        convenience methods when the caller omits an organization_id.
        """
        access_token = self.config.get("access_token")
        if not access_token:
            logger.warning(
                "hubstaff.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required",
            )

        await self.save_config(
            {
                "access_token": access_token,
                "default_organization_id": self.default_organization_id,
                "base_url": self.config.get("base_url", _HUBSTAFF_BASE),
            }
        )
        logger.info("hubstaff.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Hubstaff connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured PAT.
        """
        return TokenInfo(
            access_token=self.access_token,
            refresh_token=None,
            expires_at=None,
            token_type="Bearer",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Hubstaff API connectivity via GET /users/me."""
        try:
            await with_retry(
                lambda: self.http_client.get_current_user(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Hubstaff API reachable",
            )
        except HubstaffAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Hubstaff auth failed: {exc}",
            )
        except HubstaffNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Hubstaff network error: {exc}",
            )
        except HubstaffError as exc:
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
        """Sync Hubstaff projects + tasks + daily activities into the Shielva KB.

        Resolves the organization from `default_organization_id` (else the first
        organization the PAT has access to). For each project, enumerates tasks;
        and for the org, pulls today's-to-yesterday's daily activity slice.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            org_id = self.default_organization_id
            if org_id is None:
                orgs_resp = await with_retry(
                    lambda: self.http_client.list_organizations(page_limit=50),
                    max_retries=2,
                )
                org_list = (
                    orgs_resp.get("organizations", [])
                    if isinstance(orgs_resp, dict)
                    else []
                )
                if not org_list:
                    return SyncResult(
                        status=SyncStatus.COMPLETED,
                        documents_found=0,
                        documents_synced=0,
                        documents_failed=0,
                        message="No Hubstaff organizations available",
                    )
                org_id = org_list[0].get("id")
                if org_id is None:
                    return SyncResult(
                        status=SyncStatus.FAILED,
                        documents_found=0,
                        documents_synced=0,
                        documents_failed=0,
                        message="First organization has no id",
                    )

            # Projects
            projects_resp = await with_retry(
                lambda: self.http_client.list_projects(org_id),
                max_retries=3,
            )
            for raw in projects_resp.get("projects", []) or []:
                documents_found += 1
                try:
                    doc = normalize_project(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("hubstaff.sync.project_failed", error=str(exc))
                    documents_failed += 1

                # Tasks per project
                try:
                    tasks_resp = await with_retry(
                        lambda pid=raw.get("id"): self.http_client.list_tasks(pid),
                        max_retries=3,
                    )
                except Exception as exc:
                    logger.warning(
                        "hubstaff.sync.tasks_skip",
                        project_id=raw.get("id"),
                        error=str(exc),
                    )
                    continue
                for traw in tasks_resp.get("tasks", []) or []:
                    documents_found += 1
                    try:
                        doc = normalize_task(traw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("hubstaff.sync.task_failed", error=str(exc))
                        documents_failed += 1

            # Daily activities
            now = datetime.now(timezone.utc)
            if since is not None:
                date_start = since.date().isoformat()
            else:
                date_start = (now - timedelta(days=1)).date().isoformat()
            date_stop = now.date().isoformat()

            activities_resp = await with_retry(
                lambda: self.http_client.list_daily_activities(
                    org_id,
                    date_start=date_start,
                    date_stop=date_stop,
                    page_limit=100,
                ),
                max_retries=3,
            )
            for raw in activities_resp.get("daily_activities", []) or []:
                documents_found += 1
                try:
                    doc = normalize_activity(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("hubstaff.sync.activity_failed", error=str(exc))
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Hubstaff documents",
            )
        except Exception as exc:
            logger.error(
                "hubstaff.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /users/me."""
        return await with_retry(
            lambda: self.http_client.get_current_user(),
            max_retries=3,
        )

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        """GET /users/{id}."""
        return await with_retry(
            lambda: self.http_client.get_user(user_id),
            max_retries=3,
        )

    async def list_organizations(
        self,
        page_start_id: Optional[int] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations."""
        return await with_retry(
            lambda: self.http_client.list_organizations(
                page_start_id=page_start_id,
                page_limit=page_limit,
            ),
            max_retries=3,
        )

    async def list_users(
        self,
        organization_id: int,
        page_start_id: Optional[int] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/members."""
        return await with_retry(
            lambda: self.http_client.list_users(
                organization_id,
                page_start_id=page_start_id,
                page_limit=page_limit,
            ),
            max_retries=3,
        )

    async def list_teams(self, organization_id: int) -> Dict[str, Any]:
        """GET /organizations/{id}/teams."""
        return await with_retry(
            lambda: self.http_client.list_teams(organization_id),
            max_retries=3,
        )

    async def list_projects(
        self,
        organization_id: int,
        status: str = "active",
        page_start_id: Optional[int] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/projects."""
        return await with_retry(
            lambda: self.http_client.list_projects(
                organization_id,
                status=status,
                page_start_id=page_start_id,
                page_limit=page_limit,
            ),
            max_retries=3,
        )

    async def get_project(self, project_id: int) -> Dict[str, Any]:
        """GET /projects/{id}."""
        return await with_retry(
            lambda: self.http_client.get_project(project_id),
            max_retries=3,
        )

    async def create_project(
        self,
        organization_id: int,
        name: str,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /organizations/{id}/projects."""
        return await self.http_client.create_project(
            organization_id,
            name=name,
            description=description,
        )

    async def list_tasks(
        self,
        project_id: int,
        status: str = "open",
        page_start_id: Optional[int] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /projects/{id}/tasks."""
        return await with_retry(
            lambda: self.http_client.list_tasks(
                project_id,
                status=status,
                page_start_id=page_start_id,
                page_limit=page_limit,
            ),
            max_retries=3,
        )

    async def list_activities(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        project_ids: Optional[List[int]] = None,
        page_start_id: Optional[int] = None,
        page_limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/activities."""
        return await with_retry(
            lambda: self.http_client.list_activities(
                organization_id,
                date_start=date_start,
                date_stop=date_stop,
                user_ids=user_ids,
                project_ids=project_ids,
                page_start_id=page_start_id,
                page_limit=page_limit,
            ),
            max_retries=3,
        )

    async def list_time_entries(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        project_ids: Optional[List[int]] = None,
        page_start_id: Optional[int] = None,
        page_limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/time_entries."""
        return await with_retry(
            lambda: self.http_client.list_time_entries(
                organization_id,
                date_start=date_start,
                date_stop=date_stop,
                user_ids=user_ids,
                project_ids=project_ids,
                page_start_id=page_start_id,
                page_limit=page_limit,
            ),
            max_retries=3,
        )

    async def list_daily_activities(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        project_ids: Optional[List[int]] = None,
        page_start_id: Optional[int] = None,
        page_limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/activities/daily."""
        return await with_retry(
            lambda: self.http_client.list_daily_activities(
                organization_id,
                date_start=date_start,
                date_stop=date_stop,
                user_ids=user_ids,
                project_ids=project_ids,
                page_start_id=page_start_id,
                page_limit=page_limit,
            ),
            max_retries=3,
        )

    async def list_screenshots(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        project_ids: Optional[List[int]] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/screenshots."""
        return await with_retry(
            lambda: self.http_client.list_screenshots(
                organization_id,
                date_start=date_start,
                date_stop=date_stop,
                user_ids=user_ids,
                project_ids=project_ids,
                page_limit=page_limit,
            ),
            max_retries=3,
        )

    async def list_apps(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/application_activities."""
        return await with_retry(
            lambda: self.http_client.list_apps(
                organization_id,
                date_start=date_start,
                date_stop=date_stop,
                user_ids=user_ids,
                page_limit=page_limit,
            ),
            max_retries=3,
        )

    async def list_urls(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/url_activities."""
        return await with_retry(
            lambda: self.http_client.list_urls(
                organization_id,
                date_start=date_start,
                date_stop=date_stop,
                user_ids=user_ids,
                page_limit=page_limit,
            ),
            max_retries=3,
        )

    async def list_notes(
        self,
        organization_id: int,
        date_start: Optional[str] = None,
        date_stop: Optional[str] = None,
        user_ids: Optional[List[int]] = None,
        page_limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /organizations/{id}/notes."""
        return await with_retry(
            lambda: self.http_client.list_notes(
                organization_id,
                date_start=date_start,
                date_stop=date_stop,
                user_ids=user_ids,
                page_limit=page_limit,
            ),
            max_retries=3,
        )
