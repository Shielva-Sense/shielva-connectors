"""Snyk connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Snyk API token. The token is sent in the Authorization header with the
literal ``token`` prefix (NOT ``Bearer``):

    Authorization: token <api_token>
    Content-Type:  application/vnd.api+json   # REST v3
    Content-Type:  application/json           # legacy v1
"""
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

from client.http_client import SnykHTTPClient
from exceptions import (
    SnykAuthError,
    SnykError,
    SnykNotFoundError,
    SnykServerError,
)
from helpers.normalizer import normalize_issue, normalize_project
from helpers.utils import parse_starting_after, with_retry

logger = structlog.get_logger(__name__)

_REST_BASE = "https://api.snyk.io/rest"
_V1_BASE = "https://api.snyk.io/v1"
_DEFAULT_VERSION = "2024-10-15"


class SnykConnector(BaseConnector):
    """Shielva connector for the Snyk security & vulnerability platform."""

    CONNECTOR_TYPE = "snyk"
    CONNECTOR_NAME = "Snyk"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["api_token"]

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
        self.default_org_id: str = self.config.get("default_org_id", "")
        self.api_version: str = self.config.get("api_version", _DEFAULT_VERSION)
        self.base_url: str = self.config.get("base_url", "") or _REST_BASE
        self.v1_base_url: str = self.config.get("v1_base_url", "") or _V1_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 200)

        self.http_client = SnykHTTPClient(
            api_token=self.api_token,
            rest_base_url=self.base_url,
            v1_base_url=self.v1_base_url,
            default_version=self.api_version,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate the API token is present and persist install config.

        Mirrors the Wix-quality contract: install does NOT call the provider
        API — that is `health_check`'s job. install only validates required
        config keys.
        """
        api_token = self.config.get("api_token") or self.api_token
        if not api_token:
            logger.warning(
                "snyk.install.missing_credentials",
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
                "default_org_id": self.default_org_id,
                "api_version": self.api_version,
                "base_url": self.base_url,
                "v1_base_url": self.v1_base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("snyk.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Snyk connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returns a TokenInfo for surface compatibility whose access_token is
        the configured API token.
        """
        return TokenInfo(
            access_token=self.api_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Snyk API connectivity by fetching the token holder via /user/me."""
        if not self.api_token:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token missing",
            )
        try:
            await with_retry(
                lambda: self.http_client.get_self(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Snyk API reachable",
            )
        except SnykAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Snyk auth failed: {exc}",
            )
        except SnykServerError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Snyk network/server error: {exc}",
            )
        except SnykError as exc:
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
        """Sync projects + issues for ``default_org_id`` into the Shielva KB.

        Multi-org sweep is left to a future pass — call against one org per
        connector for now.
        """
        if not self.default_org_id:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="default_org_id is required for sync",
            )

        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Projects (paginated via starting_after cursor)
            starting_after: Optional[str] = None
            while True:
                resp = await with_retry(
                    lambda sa=starting_after: self.http_client.list_projects(
                        self.default_org_id,
                        starting_after=sa,
                    ),
                    max_retries=3,
                )
                for raw in resp.get("data", []) or []:
                    documents_found += 1
                    try:
                        doc = normalize_project(
                            {"data": raw}, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "snyk.sync.project_failed",
                            error=str(exc),
                            connector_id=self.connector_id,
                        )
                        documents_failed += 1
                next_link = (resp.get("links") or {}).get("next")
                starting_after = parse_starting_after(next_link or "")
                if not starting_after:
                    break

            # Issues (paginated via starting_after cursor)
            starting_after = None
            while True:
                resp = await with_retry(
                    lambda sa=starting_after: self.http_client.list_issues(
                        self.default_org_id,
                        starting_after=sa,
                    ),
                    max_retries=3,
                )
                for raw in resp.get("data", []) or []:
                    documents_found += 1
                    try:
                        doc = normalize_issue(
                            {"data": raw}, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "snyk.sync.issue_failed",
                            error=str(exc),
                            connector_id=self.connector_id,
                        )
                        documents_failed += 1
                next_link = (resp.get("links") or {}).get("next")
                starting_after = parse_starting_after(next_link or "")
                if not starting_after:
                    break

            return SyncResult(
                status=SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Snyk records",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "snyk.sync.failed", error=str(exc), connector_id=self.connector_id
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def list_organizations(
        self,
        version: Optional[str] = None,
        limit: int = 100,
        starting_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /rest/orgs — paginated orgs visible to the token."""
        return await with_retry(
            lambda: self.http_client.list_organizations(
                version=version,
                limit=limit,
                starting_after=starting_after,
            ),
            max_retries=3,
        )

    async def get_organization(self, org_id: str) -> Dict[str, Any]:
        """GET /rest/orgs/{org_id}."""
        return await with_retry(
            lambda: self.http_client.get_organization(org_id),
            max_retries=3,
        )

    async def list_projects(
        self,
        org_id: str,
        target_id: Optional[str] = None,
        types: Optional[List[str]] = None,
        limit: int = 100,
        starting_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /rest/orgs/{org_id}/projects."""
        return await with_retry(
            lambda: self.http_client.list_projects(
                org_id,
                target_id=target_id,
                types=types,
                limit=limit,
                starting_after=starting_after,
            ),
            max_retries=3,
        )

    async def get_project(self, org_id: str, project_id: str) -> Dict[str, Any]:
        """GET /rest/orgs/{org_id}/projects/{project_id}."""
        return await with_retry(
            lambda: self.http_client.get_project(org_id, project_id),
            max_retries=3,
        )

    async def delete_project(
        self, org_id: str, project_id: str
    ) -> Dict[str, Any]:
        """DELETE /rest/orgs/{org_id}/projects/{project_id}."""
        return await self.http_client.delete_project(org_id, project_id)

    async def list_issues(
        self,
        org_id: str,
        project_id: Optional[str] = None,
        severity: Optional[List[str]] = None,
        type: Optional[str] = None,
        limit: int = 50,
        starting_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /rest/orgs/{org_id}/issues."""
        return await with_retry(
            lambda: self.http_client.list_issues(
                org_id,
                project_id=project_id,
                severity=severity,
                type=type,
                limit=limit,
                starting_after=starting_after,
            ),
            max_retries=3,
        )

    async def get_issue(self, org_id: str, issue_id: str) -> Dict[str, Any]:
        """GET /rest/orgs/{org_id}/issues/{issue_id}."""
        return await with_retry(
            lambda: self.http_client.get_issue(org_id, issue_id),
            max_retries=3,
        )

    async def list_dependencies(
        self,
        org_id: str,
        project_id: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Legacy v1 — POST /org/{org_id}/dependencies."""
        return await with_retry(
            lambda: self.http_client.list_dependencies(
                org_id, project_id=project_id, limit=limit
            ),
            max_retries=3,
        )

    async def list_targets(
        self,
        org_id: str,
        source: Optional[str] = None,
        limit: int = 100,
        starting_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /rest/orgs/{org_id}/targets."""
        return await with_retry(
            lambda: self.http_client.list_targets(
                org_id,
                source=source,
                limit=limit,
                starting_after=starting_after,
            ),
            max_retries=3,
        )

    async def get_target(self, org_id: str, target_id: str) -> Dict[str, Any]:
        """GET /rest/orgs/{org_id}/targets/{target_id}."""
        return await with_retry(
            lambda: self.http_client.get_target(org_id, target_id),
            max_retries=3,
        )

    async def list_users(self, org_id: str) -> Dict[str, Any]:
        """GET /v1/org/{org_id}/members (legacy v1)."""
        return await with_retry(
            lambda: self.http_client.list_org_members(org_id),
            max_retries=3,
        )

    async def list_org_members(self, org_id: str) -> Dict[str, Any]:
        """Alias for :meth:`list_users` — kept for clarity in callers."""
        return await self.list_users(org_id)

    async def get_user_settings(self, org_id: str) -> Dict[str, Any]:
        """GET /v1/user/me/notification-settings/org/{org_id}."""
        return await with_retry(
            lambda: self.http_client.get_user_settings(org_id),
            max_retries=3,
        )
