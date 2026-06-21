"""Grafana connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All retry utilities → helpers/utils.py
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
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import GrafanaHTTPClient
from exceptions import (
    GrafanaAuthError,
    GrafanaError,
    GrafanaNetworkError,
)
from helpers.normalizer import normalize_dashboard
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)


class GrafanaConnector(BaseConnector):
    """Shielva connector for the Grafana HTTP API."""

    CONNECTOR_TYPE = "grafana"
    CONNECTOR_NAME = "Grafana"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "instance_url",
        "service_account_token",
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
        self.instance_url: str = (self.config.get("instance_url") or "").rstrip("/")
        self.service_account_token: str = self.config.get("service_account_token", "")
        self.org_id: Optional[int] = self.config.get("org_id")
        self.rate_limit_per_min: int = int(self.config.get("rate_limit_per_min", 300))

        self.http_client = GrafanaHTTPClient(
            base_url=self.instance_url,
            token=self.service_account_token,
        )

    # ── Abstract method implementations ────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and return connector status."""
        if not self.instance_url:
            logger.warning("grafana.install.missing_instance_url", connector_id=self.connector_id)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="instance_url is required (e.g. https://myorg.grafana.net)",
            )
        if not self.service_account_token:
            logger.warning("grafana.install.missing_token", connector_id=self.connector_id)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="service_account_token is required",
            )

        await self.save_config(
            {
                "instance_url": self.instance_url,
                "service_account_token": self.service_account_token,
                "org_id": self.org_id,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("grafana.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Connector installed — service account token configured",
        )

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """Grafana uses API key auth (no OAuth code exchange).

        The Service Account token is provided at install time and is treated as
        a static, non-expiring credential. We return a TokenInfo wrapper for
        BaseConnector parity.
        """
        token_info = TokenInfo(
            access_token=self.service_account_token,
            refresh_token=None,
            expires_at=None,
            token_type="Bearer",
            scopes=[],
        )
        await self.set_token(token_info)
        logger.info("grafana.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify Grafana API reachability + token validity via /api/health."""
        try:
            health = await with_retry(
                lambda: self.http_client.get_health(),
                max_retries=2,
            )
            db_state = health.get("database", "unknown") if isinstance(health, dict) else "unknown"
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Grafana API reachable (database={db_state})",
            )
        except GrafanaAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Token rejected: {exc}",
            )
        except GrafanaNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Network error: {exc}",
            )
        except GrafanaError as exc:
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
        """Sync Grafana dashboards into the Shielva knowledge base."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            hits = await with_retry(
                lambda: self.http_client.search_dashboards(limit=1000, page=1),
                max_retries=3,
            )
            documents_found = len(hits)

            for hit in hits:
                uid = hit.get("uid")
                if not uid:
                    continue
                try:
                    full_dash = await with_retry(
                        lambda u=uid: self.http_client.get_dashboard(u),
                        max_retries=3,
                    )
                    doc = normalize_dashboard(
                        hit,
                        full_dash,
                        self.connector_id,
                        self.tenant_id,
                        base_url=self.instance_url,
                    )
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "grafana.sync.dashboard_failed",
                        uid=uid,
                        error=str(exc),
                    )
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} dashboards",
            )

        except Exception as exc:
            logger.error("grafana.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API surface ────────────────────────────────────────────────

    async def get_org(self) -> Dict[str, Any]:
        """GET /api/org — current organization context for the auth token."""
        return await with_retry(
            lambda: self.http_client.get_org(),
            max_retries=3,
        )

    async def list_dashboards(
        self,
        limit: int = 100,
        page: int = 1,
        query: Optional[str] = None,
        tag: Optional[List[str]] = None,
        folder_uids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """GET /api/search?type=dash-db — list dashboards (optionally filtered)."""
        return await with_retry(
            lambda: self.http_client.search_dashboards(
                limit=limit, page=page, query=query, tag=tag, folder_uids=folder_uids,
            ),
            max_retries=3,
        )

    async def get_dashboard(self, uid: str) -> Dict[str, Any]:
        """GET /api/dashboards/uid/{uid}."""
        return await with_retry(
            lambda: self.http_client.get_dashboard(uid),
            max_retries=3,
        )

    async def create_dashboard(
        self,
        dashboard: Dict[str, Any],
        folder_uid: Optional[str] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """POST /api/dashboards/db — create or update a dashboard."""
        return await with_retry(
            lambda: self.http_client.post_dashboard(
                dashboard=dashboard, folder_uid=folder_uid, overwrite=overwrite,
            ),
            max_retries=3,
        )

    async def delete_dashboard(self, uid: str) -> Dict[str, Any]:
        """DELETE /api/dashboards/uid/{uid}."""
        return await with_retry(
            lambda: self.http_client.delete_dashboard(uid),
            max_retries=3,
        )

    async def list_folders(self, limit: int = 100, page: int = 1) -> List[Dict[str, Any]]:
        """GET /api/folders."""
        return await with_retry(
            lambda: self.http_client.list_folders(limit=limit, page=page),
            max_retries=3,
        )

    async def create_folder(self, title: str, uid: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/folders."""
        return await with_retry(
            lambda: self.http_client.create_folder(title=title, uid=uid),
            max_retries=3,
        )

    async def list_datasources(self) -> List[Dict[str, Any]]:
        """GET /api/datasources."""
        return await with_retry(
            lambda: self.http_client.list_datasources(),
            max_retries=3,
        )

    async def create_datasource(
        self,
        name: str,
        type: str,
        url: str,
        access: str = "proxy",
        is_default: bool = False,
        basic_auth: bool = False,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /api/datasources."""
        return await with_retry(
            lambda: self.http_client.create_datasource(
                name=name,
                type_=type,
                url=url,
                access=access,
                is_default=is_default,
                basic_auth=basic_auth,
                json_data=json_data,
            ),
            max_retries=3,
        )

    async def query_datasource(
        self,
        datasource_id: int,
        queries: List[Dict[str, Any]],
        from_time: Optional[int] = None,
        to_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /api/ds/query."""
        return await with_retry(
            lambda: self.http_client.query_datasource(
                datasource_id=datasource_id,
                queries=queries,
                from_time=from_time,
                to_time=to_time,
            ),
            max_retries=3,
        )

    async def list_alert_rules(self, limit: int = 100) -> List[Dict[str, Any]]:
        """GET /api/v1/provisioning/alert-rules."""
        return await with_retry(
            lambda: self.http_client.list_alert_rules(limit=limit),
            max_retries=3,
        )

    async def list_users(self, perpage: int = 1000, page: int = 1) -> List[Dict[str, Any]]:
        """GET /api/users."""
        return await with_retry(
            lambda: self.http_client.list_users(perpage=perpage, page=page),
            max_retries=3,
        )

    async def list_teams(
        self,
        perpage: int = 1000,
        page: int = 1,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/teams/search."""
        return await with_retry(
            lambda: self.http_client.search_teams(perpage=perpage, page=page, query=query),
            max_retries=3,
        )

    async def get_dashboard_doc(self, uid: str) -> NormalizedDocument:
        """Convenience: fetch a dashboard and return a NormalizedDocument."""
        hits = await self.list_dashboards(query=uid, limit=1)
        hit = next((h for h in hits if h.get("uid") == uid), {"uid": uid, "title": uid})
        full = await self.get_dashboard(uid)
        return normalize_dashboard(
            hit, full, self.connector_id, self.tenant_id, base_url=self.instance_url,
        )
