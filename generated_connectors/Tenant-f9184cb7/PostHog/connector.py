"""PostHog connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: two API keys.
- personal_api_key (phx_…) → Authorization: Bearer header for management API
- project_api_key  (phc_…) → embedded in JSON body as `api_key` for /capture/ + /batch/
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

from client.http_client import PostHogHTTPClient
from exceptions import (
    PostHogAuthError,
    PostHogError,
    PostHogNetworkError,
    PostHogNotFound,
)
from helpers.normalizer import normalize_event, normalize_person
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_DEFAULT_BASE = "https://app.posthog.com"


class PostHogConnector(BaseConnector):
    """Shielva connector for the PostHog REST + Capture API."""

    CONNECTOR_TYPE = "posthog"
    CONNECTOR_NAME = "PostHog"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "personal_api_key",
        "project_id",
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
        self.personal_api_key: str = self.config.get("personal_api_key", "")
        self.project_api_key: str = self.config.get("project_api_key", "")
        self.project_id: str = str(self.config.get("project_id", "") or "")
        self.base_url: str = self.config.get("base_url", "") or _DEFAULT_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 240)

        self.http_client = PostHogHTTPClient(
            personal_api_key=self.personal_api_key,
            project_api_key=self.project_api_key,
            project_id=self.project_id,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        PostHog install requires `personal_api_key` + `project_id`. The
        `project_api_key` is optional and only required when the tenant calls
        the capture surfaces (`capture_event`, `batch_capture`, `identify_person`).
        """
        personal_api_key = self.config.get("personal_api_key")
        project_id = self.config.get("project_id")

        if not personal_api_key or not project_id:
            logger.warning(
                "posthog.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="personal_api_key and project_id are required",
            )

        await self.save_config(
            {
                "personal_api_key": personal_api_key,
                "project_api_key": self.config.get("project_api_key", ""),
                "project_id": str(project_id),
                "base_url": self.config.get("base_url", _DEFAULT_BASE),
            }
        )
        logger.info("posthog.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="PostHog connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured personal_api_key.
        """
        return TokenInfo(
            access_token=self.personal_api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify PostHog API connectivity by fetching the configured project."""
        try:
            await with_retry(
                lambda: self.http_client.get_project(self.project_id),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="PostHog API reachable",
            )
        except PostHogAuthError as exc:
            health = ConnectorHealth.UNHEALTHY if exc.status_code == 403 else ConnectorHealth.DEGRADED
            auth_status = (
                AuthStatus.INVALID_CREDENTIALS
                if exc.status_code == 403
                else AuthStatus.TOKEN_EXPIRED
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=health,
                auth_status=auth_status,
                message=f"PostHog auth failed: {exc}",
            )
        except PostHogNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"PostHog network error: {exc}",
            )
        except PostHogError as exc:
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
        """Sync PostHog persons + recent events into the Shielva KB.

        Iterates persons + events for the default project, normalises each,
        and ingests one document at a time. Tenant-scoped id =
        f"{tenant_id}_{source_id}".
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            persons_resp = await with_retry(
                lambda: self.http_client.list_persons(self.project_id, limit=100),
                max_retries=3,
            )
            for raw in persons_resp.get("results", []) or []:
                documents_found += 1
                try:
                    doc = normalize_person(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error("posthog.sync.person_failed", error=str(exc))
                    documents_failed += 1

            events_resp = await with_retry(
                lambda: self.http_client.list_events(self.project_id, limit=100),
                max_retries=3,
            )
            for raw in events_resp.get("results", []) or []:
                documents_found += 1
                try:
                    doc = normalize_event(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error("posthog.sync.event_failed", error=str(exc))
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} PostHog documents",
            )
        except Exception as exc:
            logger.error(
                "posthog.sync.failed",
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

    async def list_projects(self) -> Dict[str, Any]:
        """GET /api/projects — list all projects visible to the personal key."""
        return await with_retry(
            lambda: self.http_client.list_projects(),
            max_retries=3,
        )

    async def get_project(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/projects/{project_id}."""
        return await with_retry(
            lambda: self.http_client.get_project(project_id),
            max_retries=3,
        )

    # Capture surface
    async def capture_event(
        self,
        distinct_id: str,
        event_name: str,
        properties: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /capture/ — single event with project_api_key in body."""
        if not self.project_api_key:
            raise PostHogError("project_api_key is required to capture events")
        return await with_retry(
            lambda: self.http_client.capture(
                distinct_id=distinct_id,
                event=event_name,
                properties=properties,
                timestamp=timestamp,
            ),
            max_retries=3,
        )

    async def batch_capture(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /batch/ — list of capture dicts with project_api_key at top level."""
        if not self.project_api_key:
            raise PostHogError("project_api_key is required to batch capture")
        return await with_retry(
            lambda: self.http_client.batch(events),
            max_retries=3,
        )

    async def identify_person(
        self,
        distinct_id: str,
        properties: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Send an `$identify` event — `properties` are merged into `$set`."""
        merged = {"$set": properties or {}}
        return await self.capture_event(
            distinct_id=distinct_id,
            event_name="$identify",
            properties=merged,
        )

    async def alias_distinct_ids(
        self,
        distinct_id: str,
        alias: str,
    ) -> Dict[str, Any]:
        """Send a `$create_alias` event to merge two distinct_ids on one person."""
        return await self.capture_event(
            distinct_id=distinct_id,
            event_name="$create_alias",
            properties={"alias": alias},
        )

    # Persons
    async def list_persons(
        self,
        project_id: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /api/projects/{id}/persons."""
        return await with_retry(
            lambda: self.http_client.list_persons(project_id, search=search, limit=limit),
            max_retries=3,
        )

    async def get_person(
        self,
        person_id: str,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/projects/{id}/persons/{person_id}."""
        return await with_retry(
            lambda: self.http_client.get_person(person_id, project_id=project_id),
            max_retries=3,
        )

    # Cohorts
    async def list_cohorts(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/projects/{id}/cohorts."""
        return await with_retry(
            lambda: self.http_client.list_cohorts(project_id),
            max_retries=3,
        )

    async def get_cohort(
        self,
        cohort_id: int,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/projects/{id}/cohorts/{cohort_id}."""
        return await with_retry(
            lambda: self.http_client.get_cohort(cohort_id, project_id=project_id),
            max_retries=3,
        )

    # Feature Flags
    async def list_feature_flags(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/projects/{id}/feature_flags."""
        return await with_retry(
            lambda: self.http_client.list_feature_flags(project_id),
            max_retries=3,
        )

    async def get_feature_flag(
        self,
        flag_id: int,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/projects/{id}/feature_flags/{flag_id}."""
        return await with_retry(
            lambda: self.http_client.get_feature_flag(flag_id, project_id=project_id),
            max_retries=3,
        )

    async def create_feature_flag(
        self,
        key: str,
        name: str,
        filters: Optional[Dict[str, Any]] = None,
        active: bool = True,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /api/projects/{id}/feature_flags."""
        return await self.http_client.create_feature_flag(
            key=key,
            name=name,
            filters=filters,
            active=active,
            project_id=project_id,
        )

    # Insights / Dashboards / Actions / Annotations / Experiments
    async def list_insights(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/projects/{id}/insights."""
        return await with_retry(
            lambda: self.http_client.list_insights(project_id),
            max_retries=3,
        )

    async def list_dashboards(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/projects/{id}/dashboards."""
        return await with_retry(
            lambda: self.http_client.list_dashboards(project_id),
            max_retries=3,
        )

    async def get_dashboard(
        self,
        dashboard_id: int,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/projects/{id}/dashboards/{dashboard_id}."""
        return await with_retry(
            lambda: self.http_client.get_dashboard(dashboard_id, project_id=project_id),
            max_retries=3,
        )

    async def list_actions(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/projects/{id}/actions."""
        return await with_retry(
            lambda: self.http_client.list_actions(project_id),
            max_retries=3,
        )

    async def list_annotations(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/projects/{id}/annotations."""
        return await with_retry(
            lambda: self.http_client.list_annotations(project_id),
            max_retries=3,
        )

    async def list_experiments(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/projects/{id}/experiments."""
        return await with_retry(
            lambda: self.http_client.list_experiments(project_id),
            max_retries=3,
        )

    # Events + HogQL query
    async def list_events(
        self,
        project_id: Optional[str] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /api/projects/{id}/events."""
        return await with_retry(
            lambda: self.http_client.list_events(
                project_id, after=after, before=before, limit=limit
            ),
            max_retries=3,
        )

    async def run_query(
        self,
        query: Dict[str, Any],
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /api/projects/{id}/query — arbitrary HogQL/insight query."""
        return await with_retry(
            lambda: self.http_client.run_query(query, project_id=project_id),
            max_retries=3,
        )
