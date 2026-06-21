"""YouTrack connector — orchestration only.

All HTTP calls    → client/http_client.py
All normalization → helpers/normalizer.py
All utilities     → helpers/utils.py

Auth: permanent token, sent as ``Authorization: Bearer <perm:...>``.
Per-tenant ``base_url`` because YouTrack lives on a per-instance host
(``https://{org}.youtrack.cloud/api`` or self-hosted ``/api``).
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import YouTrackHTTPClient
from exceptions import (
    YouTrackAuthError,
    YouTrackError,
    YouTrackNetworkError,
    YouTrackNotFound,
)
from helpers.normalizer import normalize_issue
from helpers.utils import normalize_base_url, with_retry

logger = structlog.get_logger(__name__)


class YouTrackConnector(BaseConnector):
    """Shielva connector for the JetBrains YouTrack REST API."""

    CONNECTOR_TYPE = "youtrack"
    CONNECTOR_NAME = "YouTrack"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "base_url",
        "permanent_token",
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
        self.base_url: str = self.config.get("base_url", "") or ""
        self.permanent_token: str = self.config.get("permanent_token", "") or ""
        self.default_project_id: str = self.config.get("default_project_id", "") or ""
        self.rate_limit_per_min: int = int(self.config.get("rate_limit_per_min", 200) or 200)

        self._api_base: str = normalize_base_url(self.base_url)
        self.http_client: YouTrackHTTPClient = YouTrackHTTPClient(
            base_url=self._api_base or "https://example.youtrack.cloud/api",
            permanent_token=self.permanent_token,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and probe ``/users/me?fields=login``."""
        if not self.base_url:
            logger.warning(
                "youtrack.install.missing_base_url",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="base_url is required",
            )
        if not self.permanent_token:
            logger.warning(
                "youtrack.install.missing_token",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="permanent_token is required",
            )

        # Rebuild the HTTP client now that we have both values.
        self._api_base = normalize_base_url(self.base_url)
        self.http_client = YouTrackHTTPClient(
            base_url=self._api_base,
            permanent_token=self.permanent_token,
        )

        try:
            await self.http_client.get_current_user(fields="login")
        except YouTrackAuthError as exc:
            logger.warning("youtrack.install.auth_failed", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Token rejected by YouTrack: {exc}",
            )
        except YouTrackNetworkError as exc:
            logger.warning("youtrack.install.network_error", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.PENDING,
                message=f"Cannot reach YouTrack: {exc}",
            )
        except YouTrackError as exc:
            logger.warning("youtrack.install.error", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=str(exc),
            )

        await self.save_config(
            {
                "base_url": self.base_url,
                "permanent_token": self.permanent_token,
                "default_project_id": self.default_project_id,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("youtrack.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="YouTrack connector installed and authenticated",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        ``TokenInfo`` whose ``access_token`` is the configured permanent token.
        """
        return TokenInfo(
            access_token=self.permanent_token,
            refresh_token=None,
            expires_at=None,
            token_type="permanent_token",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Probe ``/users/me?fields=login`` to verify the token is still valid."""
        try:
            await with_retry(
                lambda: self.http_client.get_current_user(fields="login"),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="YouTrack API reachable",
            )
        except YouTrackAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Token rejected: {exc}",
            )
        except YouTrackNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"YouTrack network error: {exc}",
            )
        except YouTrackError as exc:
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
        """Page through ``/issues`` and ingest each one as a NormalizedDocument."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            query_parts: List[str] = []
            if self.default_project_id:
                query_parts.append(f"project: {self.default_project_id}")
            if since and not full:
                query_parts.append(f"updated: {since.strftime('%Y-%m-%d')} .. *")
            query = " ".join(query_parts) if query_parts else None

            skip = 0
            page_size = 100
            while True:
                page = await with_retry(
                    lambda s=skip: self.http_client.list_issues(
                        query=query, skip=s, top=page_size
                    ),
                    max_retries=3,
                )
                if not page:
                    break
                documents_found += len(page)
                for raw in page:
                    try:
                        doc = normalize_issue(
                            raw,
                            connector_id=self.connector_id,
                            tenant_id=self.tenant_id,
                            base_url=self._api_base,
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "youtrack.sync.issue_failed",
                            issue_id=raw.get("id") if isinstance(raw, dict) else None,
                            error=str(exc),
                        )
                        documents_failed += 1
                if len(page) < page_size:
                    break
                skip += page_size

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} YouTrack issues",
            )
        except Exception as exc:
            logger.error(
                "youtrack.sync.failed", error=str(exc), connector_id=self.connector_id
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per metadata/connector.json) ───────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /users/me — current user profile."""
        return await with_retry(
            lambda: self.http_client.get_current_user(),
            max_retries=3,
        )

    async def list_users(
        self,
        query: Optional[str] = None,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,login,fullName,email,banned",
    ) -> List[Dict[str, Any]]:
        """GET /users — list users matching ``query``."""
        return await with_retry(
            lambda: self.http_client.list_users(
                query=query, skip=skip, top=top, fields=fields
            ),
            max_retries=3,
        )

    async def get_user(
        self,
        user_id: str,
        fields: Optional[str] = "id,login,fullName,email,banned",
    ) -> Dict[str, Any]:
        """GET /users/{id}."""
        if not user_id:
            raise ValueError("user_id is required")
        return await with_retry(
            lambda: self.http_client.get_user(user_id, fields=fields),
            max_retries=3,
        )

    async def list_projects(
        self,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,shortName,name,description,archived",
    ) -> List[Dict[str, Any]]:
        """GET /admin/projects — list projects visible to the token."""
        return await with_retry(
            lambda: self.http_client.list_projects(skip=skip, top=top, fields=fields),
            max_retries=3,
        )

    async def get_project(
        self,
        project_id: str,
        fields: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /admin/projects/{id}."""
        if not project_id:
            raise ValueError("project_id is required")
        return await with_retry(
            lambda: self.http_client.get_project(project_id, fields=fields),
            max_retries=3,
        )

    async def list_issues(
        self,
        query: Optional[str] = None,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,idReadable,summary,description,created,updated,reporter(login),customFields(name,value(name))",
    ) -> List[Dict[str, Any]]:
        """GET /issues — list issues matching the YouTrack query language."""
        return await with_retry(
            lambda: self.http_client.list_issues(
                query=query, skip=skip, top=top, fields=fields
            ),
            max_retries=3,
        )

    async def get_issue(
        self,
        issue_id: str,
        fields: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /issues/{id}."""
        if not issue_id:
            raise ValueError("issue_id is required")
        return await with_retry(
            lambda: self.http_client.get_issue(issue_id, fields=fields),
            max_retries=3,
        )

    async def create_issue(
        self,
        project_id: str,
        summary: str,
        description: str = "",
        custom_fields: Optional[List[Dict[str, Any]]] = None,
        fields: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /issues — create an issue in ``project_id``."""
        if not project_id:
            project_id = self.default_project_id
        if not project_id:
            raise ValueError("project_id is required to create an issue")
        if not summary:
            raise ValueError("summary is required to create an issue")
        return await self.http_client.create_issue(
            project_id=project_id,
            summary=summary,
            description=description,
            custom_fields=custom_fields,
            fields=fields,
        )

    async def update_issue(
        self,
        issue_id: str,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        custom_fields: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """POST /issues/{id} — update mutable fields of an issue."""
        if not issue_id:
            raise ValueError("issue_id is required")
        return await self.http_client.update_issue(
            issue_id=issue_id,
            summary=summary,
            description=description,
            custom_fields=custom_fields,
        )

    async def delete_issue(self, issue_id: str) -> Dict[str, Any]:
        """DELETE /issues/{id} — permanently delete an issue."""
        if not issue_id:
            raise ValueError("issue_id is required")
        return await self.http_client.delete_issue(issue_id)

    async def add_comment(self, issue_id: str, text: str) -> Dict[str, Any]:
        """POST /issues/{id}/comments — add a comment."""
        if not issue_id:
            raise ValueError("issue_id is required")
        if not text:
            raise ValueError("text is required")
        return await self.http_client.add_comment(issue_id, text)

    async def list_comments(
        self,
        issue_id: str,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,text,author(login),created",
    ) -> List[Dict[str, Any]]:
        """GET /issues/{id}/comments — list comments on an issue."""
        if not issue_id:
            raise ValueError("issue_id is required")
        return await with_retry(
            lambda: self.http_client.list_comments(
                issue_id, skip=skip, top=top, fields=fields
            ),
            max_retries=3,
        )

    async def list_tags(
        self,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,name,owner(login)",
    ) -> List[Dict[str, Any]]:
        """GET /issueTags — list account-wide tags."""
        return await with_retry(
            lambda: self.http_client.list_tags(skip=skip, top=top, fields=fields),
            max_retries=3,
        )

    async def list_time_tracking(
        self,
        issue_id: str,
        skip: int = 0,
        top: int = 100,
    ) -> List[Dict[str, Any]]:
        """GET /issues/{id}/timeTracking/workItems."""
        if not issue_id:
            raise ValueError("issue_id is required")
        return await with_retry(
            lambda: self.http_client.list_time_tracking(
                issue_id, skip=skip, top=top
            ),
            max_retries=3,
        )

    async def list_boards(
        self,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,name,owner(login),projects(id,shortName)",
    ) -> List[Dict[str, Any]]:
        """GET /agiles — list agile boards."""
        return await with_retry(
            lambda: self.http_client.list_boards(skip=skip, top=top, fields=fields),
            max_retries=3,
        )

    async def list_sprints(
        self,
        board_id: str,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,name,start,finish,goal,archived",
    ) -> List[Dict[str, Any]]:
        """GET /agiles/{agileId}/sprints — list sprints on a board."""
        if not board_id:
            raise ValueError("board_id is required")
        return await with_retry(
            lambda: self.http_client.list_sprints(
                board_id, skip=skip, top=top, fields=fields
            ),
            max_retries=3,
        )

    async def list_articles(
        self,
        query: Optional[str] = None,
        skip: int = 0,
        top: int = 100,
        fields: str = "id,idReadable,summary,content,project(id,shortName),reporter(login),created,updated",
    ) -> List[Dict[str, Any]]:
        """GET /articles — list knowledge-base articles."""
        return await with_retry(
            lambda: self.http_client.list_articles(
                query=query, skip=skip, top=top, fields=fields
            ),
            max_retries=3,
        )

    # ── Convenience: normalized fetch ──────────────────────────────────────

    async def get_issue_normalized(self, issue_id: str) -> NormalizedDocument:
        """Fetch an issue and return it as a NormalizedDocument."""
        if not issue_id:
            raise ValueError("issue_id is required")
        raw = await self.http_client.get_issue(
            issue_id,
            fields="id,idReadable,summary,description,created,updated,reporter(login),customFields(name,value(name))",
        )
        return normalize_issue(
            raw,
            connector_id=self.connector_id,
            tenant_id=self.tenant_id,
            base_url=self._api_base,
        )
