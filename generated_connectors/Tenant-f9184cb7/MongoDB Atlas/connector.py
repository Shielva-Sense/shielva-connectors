"""MongoDB Atlas connector — orchestration only.

All HTTP calls       → client/http_client.py
All normalization    → helpers/normalizer.py
All utilities        → helpers/utils.py
All custom errors    → exceptions.py

Atlas Admin API v2 (control plane: clusters, projects, DB users, network access,
snapshots, alerts, API keys, billing). The MongoDB wire protocol (data plane)
is out of scope.

Auth: HTTP Digest (RFC 7616) with (public_key, private_key) — Atlas issues
these from Organization → Access Manager → API Keys. ``httpx.DigestAuth``
handles the 401 → challenge → 200 dance transparently.
"""
from __future__ import annotations

from datetime import datetime, timezone
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

from client.http_client import MongoDBAtlasHTTPClient
from exceptions import (
    MongoDBAtlasAuthError,
    MongoDBAtlasError,
    MongoDBAtlasNetworkError,
    MongoDBAtlasNotFoundError,
    MongoDBAtlasRateLimitError,
)
from helpers.utils import build_cluster_payload, build_database_user_payload, with_retry

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://cloud.mongodb.com/api/atlas/v2"
_DEFAULT_API_VERSION = "2025-03-12"


class MongoDBAtlasConnector(BaseConnector):
    """Shielva connector for the MongoDB Atlas Admin API v2."""

    CONNECTOR_TYPE = "mongodb_atlas"
    CONNECTOR_NAME = "MongoDB Atlas"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "public_key",
        "private_key",
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
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        self.public_key: str = self.config.get("public_key", "")
        self.private_key: str = self.config.get("private_key", "")
        self.base_url: str = self.config.get("base_url") or _DEFAULT_BASE_URL
        self.api_version: str = self.config.get("api_version") or _DEFAULT_API_VERSION
        self.default_org_id: str = self.config.get("default_org_id", "")
        self.default_project_id: str = self.config.get("default_project_id", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        self.http_client = MongoDBAtlasHTTPClient(
            public_key=self.public_key,
            private_key=self.private_key,
            base_url=self.base_url,
            api_version=self.api_version,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Atlas Digest install requires `public_key` + `private_key` — both are
        sent on every request as the (username, password) Digest pair. We do
        NOT call the API here; that is the job of `health_check()`.
        """
        if not self.public_key or not self.private_key:
            logger.warning(
                "mongodb_atlas.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="public_key and private_key are required",
            )

        await self.save_config(
            {
                "public_key": self.public_key,
                "base_url": self.base_url,
                "api_version": self.api_version,
                "default_org_id": self.default_org_id,
                "default_project_id": self.default_project_id,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("mongodb_atlas.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="MongoDB Atlas connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """Digest auth has no code exchange — return an empty TokenInfo for ABI compat."""
        return TokenInfo(
            access_token="",
            refresh_token=None,
            expires_at=None,
            token_type="digest",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Digest credentials by hitting GET /orgs?itemsPerPage=1."""
        try:
            await with_retry(self.http_client.health_probe, max_retries=2)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="MongoDB Atlas admin API reachable",
            )
        except MongoDBAtlasAuthError as exc:
            # 401 → OFFLINE/TOKEN_EXPIRED; 403 → UNHEALTHY/INVALID_CREDENTIALS.
            if getattr(exc, "status_code", 0) == 401:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=str(exc),
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except MongoDBAtlasRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except MongoDBAtlasNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.AUTHENTICATED,
                message=str(exc),
            )
        except MongoDBAtlasError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.AUTHENTICATED,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Atlas admin API is a control plane — no documents to ingest.

        Returns a clean ``COMPLETED`` with zero documents to keep the platform
        sync contract happy. Callers that want alerts / clusters projected
        into a KB should call ``list_alerts`` / ``list_clusters`` directly and
        use ``helpers.normalizer.normalize_alert`` / ``normalize_cluster``.
        """
        return SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=0,
            documents_synced=0,
            documents_failed=0,
            message="MongoDB Atlas admin API has no documents to sync",
        )

    # ── Organizations ──────────────────────────────────────────────────────

    async def list_organizations(
        self, page_num: int = 1, items_per_page: int = 100
    ) -> Dict[str, Any]:
        """List Atlas organizations accessible to this API key."""
        return await with_retry(
            lambda: self.http_client.list_organizations(page_num, items_per_page),
            max_retries=2,
        )

    async def get_organization(self, org_id: str) -> Dict[str, Any]:
        """Fetch a single Atlas organization by id."""
        return await with_retry(
            lambda: self.http_client.get_organization(org_id),
            max_retries=2,
        )

    # ── Projects (a.k.a. Groups) ───────────────────────────────────────────

    async def list_projects(
        self, page_num: int = 1, items_per_page: int = 100
    ) -> Dict[str, Any]:
        """List Atlas projects accessible to this API key."""
        return await with_retry(
            lambda: self.http_client.list_projects(page_num, items_per_page),
            max_retries=2,
        )

    async def get_project(self, project_id: str) -> Dict[str, Any]:
        """Fetch a single Atlas project by id."""
        return await with_retry(
            lambda: self.http_client.get_project(project_id),
            max_retries=2,
        )

    async def create_project(
        self,
        name: str,
        org_id: str,
        with_default_alerts_settings: bool = True,
    ) -> Dict[str, Any]:
        """Create a new Atlas project under *org_id*."""
        body: Dict[str, Any] = {
            "name": name,
            "orgId": org_id,
            "withDefaultAlertsSettings": with_default_alerts_settings,
        }
        return await self.http_client.create_project(body)

    async def delete_project(self, project_id: str) -> Dict[str, Any]:
        """Delete an Atlas project. Irreversible — caller must confirm."""
        return await self.http_client.delete_project(project_id)

    # ── Clusters ───────────────────────────────────────────────────────────

    async def list_clusters(self, project_id: str) -> Dict[str, Any]:
        """List all clusters in *project_id*."""
        return await with_retry(
            lambda: self.http_client.list_clusters(project_id),
            max_retries=2,
        )

    async def get_cluster(self, project_id: str, cluster_name: str) -> Dict[str, Any]:
        """Fetch a single cluster by name."""
        return await with_retry(
            lambda: self.http_client.get_cluster(project_id, cluster_name),
            max_retries=2,
        )

    async def create_cluster(
        self,
        project_id: str,
        name: str,
        cluster_type: str = "REPLICASET",
        provider_settings: Optional[Dict[str, Any]] = None,
        num_shards: int = 1,
        mongo_db_major_version: str = "7.0",
    ) -> Dict[str, Any]:
        """Create a new Atlas cluster in *project_id*.

        Defaults to a 3-node REPLICASET on AWS us-east-1 / M10 / MongoDB 7.0
        when *provider_settings* is omitted. Callers should pass explicit
        settings for production deployments.
        """
        body = build_cluster_payload(
            name=name,
            cluster_type=cluster_type,
            provider_settings=provider_settings,
            num_shards=num_shards,
            mongo_db_major_version=mongo_db_major_version,
        )
        return await self.http_client.create_cluster(project_id, body)

    async def modify_cluster(
        self,
        project_id: str,
        cluster_name: str,
        patch: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply a partial PATCH to an Atlas cluster."""
        return await self.http_client.modify_cluster(project_id, cluster_name, patch)

    async def delete_cluster(
        self, project_id: str, cluster_name: str
    ) -> Dict[str, Any]:
        """Tear down an Atlas cluster. Irreversible — caller must confirm."""
        return await self.http_client.delete_cluster(project_id, cluster_name)

    # ── Database users ─────────────────────────────────────────────────────

    async def list_database_users(
        self, project_id: str, items_per_page: int = 100
    ) -> Dict[str, Any]:
        """List database users for *project_id*."""
        return await with_retry(
            lambda: self.http_client.list_database_users(project_id, items_per_page),
            max_retries=2,
        )

    async def create_database_user(
        self,
        project_id: str,
        username: str,
        password: str,
        database_name: str = "admin",
        roles: Optional[List[Dict[str, Any]]] = None,
        scopes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Create a SCRAM database user in *project_id*."""
        body = build_database_user_payload(
            username=username,
            password=password,
            database_name=database_name,
            roles=roles,
            scopes=scopes,
        )
        return await self.http_client.create_database_user(project_id, body)

    async def delete_database_user(
        self,
        project_id: str,
        username: str,
        database_name: str = "admin",
    ) -> Dict[str, Any]:
        """Delete a database user in *project_id*."""
        return await self.http_client.delete_database_user(
            project_id, database_name, username
        )

    # ── Network access list ────────────────────────────────────────────────

    async def list_network_access(self, project_id: str) -> Dict[str, Any]:
        """List the IP access list for *project_id*."""
        return await with_retry(
            lambda: self.http_client.list_network_access(project_id),
            max_retries=2,
        )

    async def add_network_access(
        self, project_id: str, entries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Add IP/CIDR entries to the project access list.

        Each entry should be of the form
        ``{"cidrBlock": "10.0.0.0/24", "comment": "office-vpn"}`` or
        ``{"ipAddress": "1.2.3.4", "comment": "bastion"}``.
        """
        return await self.http_client.add_network_access(project_id, entries)

    # ── Cloud Backup snapshots ─────────────────────────────────────────────

    async def list_snapshots(
        self, project_id: str, cluster_name: str
    ) -> Dict[str, Any]:
        """List Cloud Backup snapshots for *cluster_name* in *project_id*."""
        return await with_retry(
            lambda: self.http_client.list_snapshots(project_id, cluster_name),
            max_retries=2,
        )

    # ── Alerts ─────────────────────────────────────────────────────────────

    async def list_alerts(
        self,
        project_id: str,
        status: Optional[str] = None,
        page_num: int = 1,
        items_per_page: int = 100,
    ) -> Dict[str, Any]:
        """List alerts for *project_id*, optionally filtered by status (OPEN/CLOSED/TRACKING)."""
        return await with_retry(
            lambda: self.http_client.list_alerts(
                project_id, status=status, page_num=page_num, items_per_page=items_per_page
            ),
            max_retries=2,
        )

    # ── Programmatic API keys ──────────────────────────────────────────────

    async def list_api_keys(
        self,
        org_id: str,
        page_num: int = 1,
        items_per_page: int = 100,
    ) -> Dict[str, Any]:
        """List org-level programmatic API keys (read-only)."""
        return await with_retry(
            lambda: self.http_client.list_api_keys(org_id, page_num, items_per_page),
            max_retries=2,
        )

    # ── Billing ────────────────────────────────────────────────────────────

    async def list_invoices(
        self,
        org_id: str,
        page_num: int = 1,
        items_per_page: int = 100,
    ) -> Dict[str, Any]:
        """List org-level invoices (billing)."""
        return await with_retry(
            lambda: self.http_client.list_invoices(org_id, page_num, items_per_page),
            max_retries=2,
        )


__all__ = ["MongoDBAtlasConnector"]

# Touch the import-time timestamp — useful for debugging hot reloads.
_LOADED_AT = datetime.now(timezone.utc).isoformat()
