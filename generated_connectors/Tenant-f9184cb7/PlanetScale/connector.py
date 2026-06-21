"""PlanetScale connector — orchestration only.

All HTTP calls     → client/http_client.py
All normalization  → helpers/normalizer.py
All utilities      → helpers/utils.py

Auth: service-token. The PlanetScale REST API expects the literal
``id:token`` combo in the Authorization header — there is NO `Bearer` prefix
and NO OAuth dance:

    Authorization: <service_token_id>:<service_token>
    Accept:        application/json
    Content-Type:  application/json
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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

from client.http_client import PlanetScaleHTTPClient
from exceptions import (
    PlanetScaleAuthError,
    PlanetScaleError,
    PlanetScaleNetworkError,
    PlanetScaleNotFoundError,
    PlanetScaleRateLimitError,
)
from helpers.normalizer import normalize_branch, normalize_database
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_PLANETSCALE_BASE = "https://api.planetscale.com/v1"


class PlanetScaleConnector(BaseConnector):
    """Shielva connector for the PlanetScale (serverless MySQL) REST API.

    Surfaces Organizations, Databases, Branches, Deploy Requests, Backups, and
    Database Tokens. All methods are standalone `async def` so callers (the
    Shielva action runtime, the agentic builder, etc.) can pick the operation
    they need without inheriting orchestration boilerplate.
    """

    CONNECTOR_TYPE = "planetscale"
    CONNECTOR_NAME = "PlanetScale"
    AUTH_TYPE = "api_key"

    REQUIRED_SCOPES: List[str] = []

    # Public — the gateway / installer reads this to drive install_field
    # validation before construction.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "service_token_id",
        "service_token",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    # health_check() consults this to project provider errors back to the
    # gateway's standard status surface.
    _STATUS_MAP: Dict[int, Tuple[str, str]] = {
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
        self.service_token_id: str = self.config.get("service_token_id", "")
        self.service_token: str = self.config.get("service_token", "")
        self.base_url: str = self.config.get("base_url", "") or _PLANETSCALE_BASE
        self.default_organization: str = self.config.get("default_organization", "")
        self.default_database: str = self.config.get("default_database", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        self.http_client = PlanetScaleHTTPClient(
            service_token_id=self.service_token_id,
            service_token=self.service_token,
            base_url=self.base_url,
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _resolve_org(self, organization: Optional[str]) -> str:
        org = organization or self.default_organization
        if not org:
            raise PlanetScaleError(
                "organization is required (pass as arg or set default_organization in config)"
            )
        return org

    def _resolve_db(self, database: Optional[str]) -> str:
        db = database or self.default_database
        if not db:
            raise PlanetScaleError(
                "database is required (pass as arg or set default_database in config)"
            )
        return db

    # ── BaseConnector abstract surface ────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        PlanetScale service-token install only requires `service_token_id` and
        `service_token`. The defaults for `organization` and `database` are
        optional and used as per-call fallbacks.
        """
        service_token_id = self.config.get("service_token_id")
        service_token = self.config.get("service_token")

        if not service_token_id or not service_token:
            logger.warning(
                "planetscale.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="service_token_id and service_token are required",
            )

        await self.save_config(
            {
                "service_token_id": service_token_id,
                "service_token": service_token,
                "default_organization": self.config.get("default_organization", ""),
                "default_database": self.config.get("default_database", ""),
                "base_url": self.config.get("base_url", _PLANETSCALE_BASE),
            }
        )
        logger.info("planetscale.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="PlanetScale connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """Service-token connector — no OAuth code exchange.

        Returns a synthetic ``TokenInfo`` carrying ``service_token_id:service_token``
        so the platform can persist the credential under the standard token
        surface (Redis token store, audit log lineage).
        """
        if not self.service_token_id or not self.service_token:
            raise PlanetScaleAuthError(
                "service_token_id and service_token must be set in config before authorize()"
            )
        token_info = TokenInfo(
            access_token=f"{self.service_token_id}:{self.service_token}",
            refresh_token=None,
            expires_at=None,
            token_type="ServiceToken",
            scopes=[],
        )
        await self.set_token(token_info)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify PlanetScale API connectivity by listing one page of organizations."""
        try:
            await with_retry(
                lambda: self.http_client.list_organizations(page=1, per_page=1),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="PlanetScale API reachable",
            )
        except PlanetScaleAuthError as exc:
            # 401 / 403 — service-token mismatch or insufficient scope.
            if exc.status_code == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"PlanetScale auth failed: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"PlanetScale auth failed: {exc}",
            )
        except PlanetScaleRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"PlanetScale rate limited: {exc}",
            )
        except PlanetScaleNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"PlanetScale transient error: {exc}",
            )
        except PlanetScaleError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Sync PlanetScale databases (per org) into the Shielva KB.

        For each accessible organization we page through its databases and
        ingest each as a NormalizedDocument. No checkpoint state — PlanetScale
        does not expose a server-side "since" cursor on databases.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            if self.default_organization:
                org_names: List[str] = [self.default_organization]
            else:
                org_resp = await with_retry(
                    lambda: self.http_client.list_organizations(page=1, per_page=50),
                    max_retries=2,
                )
                org_names = [
                    org.get("name")
                    for org in (org_resp.get("data", []) or [])
                    if org.get("name")
                ]

            for org_name in org_names:
                page = 1
                while True:
                    resp = await with_retry(
                        lambda o=org_name, p=page: self.http_client.list_databases(
                            organization=o, page=p, per_page=25
                        ),
                        max_retries=3,
                    )
                    databases = resp.get("data", []) or []
                    if not databases:
                        break
                    documents_found += len(databases)
                    for db in databases:
                        try:
                            doc = normalize_database(db, self.connector_id, self.tenant_id)
                            await self.ingest_document(
                                doc, kb_id=kb_id or "", webhook_url=webhook_url
                            )
                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "planetscale.sync.database_failed",
                                org=org_name,
                                database=db.get("name"),
                                error=str(exc),
                            )
                            documents_failed += 1

                    if not resp.get("has_next") or len(databases) < 25:
                        break
                    page += 1

            status = (
                SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} databases",
            )
        except Exception as exc:
            logger.error(
                "planetscale.sync.failed",
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

    # ── Public API methods (per provider spec) ────────────────────────────

    async def list_organizations(
        self,
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """GET /organizations — list orgs the service token can see."""
        return await with_retry(
            lambda: self.http_client.list_organizations(page=page, per_page=per_page),
            max_retries=3,
        )

    async def get_organization(self, name: str) -> Dict[str, Any]:
        """GET /organizations/{name}."""
        if not name:
            raise PlanetScaleError("organization name is required")
        return await with_retry(
            lambda: self.http_client.get_organization(name),
            max_retries=3,
        )

    async def list_databases(
        self,
        organization: str = "",
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases."""
        org = self._resolve_org(organization)
        return await with_retry(
            lambda: self.http_client.list_databases(
                organization=org, page=page, per_page=per_page
            ),
            max_retries=3,
        )

    async def get_database(
        self,
        organization: str = "",
        name: str = "",
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{name}."""
        org = self._resolve_org(organization)
        if not name:
            raise PlanetScaleError("database name is required")
        return await with_retry(
            lambda: self.http_client.get_database(organization=org, name=name),
            max_retries=3,
        )

    async def create_database(
        self,
        organization: str = "",
        name: str = "",
        plan: str = "hobby",
        cluster_size: str = "PS_10",
        region: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /organizations/{org}/databases."""
        org = self._resolve_org(organization)
        if not name:
            raise PlanetScaleError("database name is required")
        return await self.http_client.create_database(
            organization=org,
            name=name,
            plan=plan,
            cluster_size=cluster_size,
            region=region,
        )

    async def delete_database(
        self,
        organization: str = "",
        name: str = "",
    ) -> Dict[str, Any]:
        """DELETE /organizations/{org}/databases/{name}."""
        org = self._resolve_org(organization)
        if not name:
            raise PlanetScaleError("database name is required")
        return await self.http_client.delete_database(organization=org, name=name)

    async def list_branches(
        self,
        organization: str = "",
        database: str = "",
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/branches."""
        org = self._resolve_org(organization)
        db = self._resolve_db(database)
        return await with_retry(
            lambda: self.http_client.list_branches(
                organization=org, database=db, page=page, per_page=per_page
            ),
            max_retries=3,
        )

    async def get_branch(
        self,
        organization: str = "",
        database: str = "",
        name: str = "",
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/branches/{name}."""
        org = self._resolve_org(organization)
        db = self._resolve_db(database)
        if not name:
            raise PlanetScaleError("branch name is required")
        return await with_retry(
            lambda: self.http_client.get_branch(
                organization=org, database=db, name=name
            ),
            max_retries=3,
        )

    async def create_branch(
        self,
        organization: str = "",
        database: str = "",
        name: str = "",
        parent_branch: str = "main",
        backup_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /organizations/{org}/databases/{db}/branches."""
        org = self._resolve_org(organization)
        db = self._resolve_db(database)
        if not name:
            raise PlanetScaleError("branch name is required")
        return await self.http_client.create_branch(
            organization=org,
            database=db,
            name=name,
            parent_branch=parent_branch,
            backup_id=backup_id,
        )

    async def delete_branch(
        self,
        organization: str = "",
        database: str = "",
        name: str = "",
    ) -> Dict[str, Any]:
        """DELETE /organizations/{org}/databases/{db}/branches/{name}."""
        org = self._resolve_org(organization)
        db = self._resolve_db(database)
        if not name:
            raise PlanetScaleError("branch name is required")
        return await self.http_client.delete_branch(
            organization=org, database=db, name=name
        )

    async def list_deploy_requests(
        self,
        organization: str = "",
        database: str = "",
        state: Optional[str] = None,
        page: int = 1,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/deploy-requests."""
        org = self._resolve_org(organization)
        db = self._resolve_db(database)
        return await with_retry(
            lambda: self.http_client.list_deploy_requests(
                organization=org, database=db, state=state, page=page
            ),
            max_retries=3,
        )

    async def get_deploy_request(
        self,
        organization: str = "",
        database: str = "",
        number: int = 0,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/deploy-requests/{n}."""
        org = self._resolve_org(organization)
        db = self._resolve_db(database)
        if not number:
            raise PlanetScaleError("deploy-request number is required")
        return await with_retry(
            lambda: self.http_client.get_deploy_request(
                organization=org, database=db, number=number
            ),
            max_retries=3,
        )

    async def create_deploy_request(
        self,
        organization: str = "",
        database: str = "",
        branch: str = "",
        into_branch: str = "main",
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /organizations/{org}/databases/{db}/deploy-requests."""
        org = self._resolve_org(organization)
        db = self._resolve_db(database)
        if not branch:
            raise PlanetScaleError("branch is required")
        return await self.http_client.create_deploy_request(
            organization=org,
            database=db,
            branch=branch,
            into_branch=into_branch,
            notes=notes,
        )

    async def list_backups(
        self,
        organization: str = "",
        database: str = "",
        branch: str = "",
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/branches/{br}/backups."""
        org = self._resolve_org(organization)
        db = self._resolve_db(database)
        if not branch:
            raise PlanetScaleError("branch is required")
        return await with_retry(
            lambda: self.http_client.list_backups(
                organization=org, database=db, branch=branch,
                page=page, per_page=per_page,
            ),
            max_retries=3,
        )

    async def list_database_tokens(
        self,
        organization: str = "",
        database: str = "",
        branch: str = "",
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/branches/{br}/passwords.

        PlanetScale's "database tokens" surface — dashboard label diverges from
        the underlying `/passwords` route.
        """
        org = self._resolve_org(organization)
        db = self._resolve_db(database)
        if not branch:
            raise PlanetScaleError("branch is required")
        return await with_retry(
            lambda: self.http_client.list_database_tokens(
                organization=org, database=db, branch=branch,
                page=page, per_page=per_page,
            ),
            max_retries=3,
        )
