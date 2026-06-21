"""Weaviate connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: API key (Weaviate Cloud / self-hosted with api-key auth module).
Sent as `Authorization: Bearer <api_key>`. Anonymous self-hosted clusters
omit the header entirely.

Per-tenant: `base_url` is an install_field (every cluster has its own URL),
NEVER a class constant.
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

from client.http_client import WeaviateHTTPClient
from exceptions import (
    WeaviateAuthError,
    WeaviateError,
    WeaviateNetworkError,
    WeaviateNotFoundError,
)
from helpers.normalizer import normalize_object
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)


class WeaviateConnector(BaseConnector):
    """Shielva connector for the Weaviate REST + GraphQL API."""

    CONNECTOR_TYPE = "weaviate"
    CONNECTOR_NAME = "Weaviate"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "base_url",
        "api_key",
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
        self.base_url: str = (self.config.get("base_url") or "").rstrip("/")
        self.api_key: str = self.config.get("api_key", "")
        self.grpc_port: Any = self.config.get("grpc_port", 50051)
        self.timeout_s: float = float(self.config.get("timeout_s", 30))

        # http_client is created when base_url is present; install() validates.
        self.http_client: Optional[WeaviateHTTPClient] = None
        if self.base_url:
            self.http_client = WeaviateHTTPClient(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout_s,
            )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Only `base_url` is hard-required. `api_key` is optional because
        self-hosted Weaviate clusters can run anonymously (no auth module).
        """
        base_url = (self.config.get("base_url") or "").strip()

        if not base_url:
            logger.warning(
                "weaviate.install.missing_base_url",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="base_url is required",
            )

        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            logger.warning(
                "weaviate.install.invalid_base_url",
                connector_id=self.connector_id,
                base_url=base_url,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="base_url must start with http:// or https://",
            )

        self.base_url = base_url.rstrip("/")
        self.http_client = WeaviateHTTPClient(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_s,
        )

        await self.save_config(
            {
                "base_url": self.base_url,
                "api_key": self.api_key,
                "grpc_port": self.grpc_port,
                "timeout_s": self.timeout_s,
            }
        )
        logger.info("weaviate.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Weaviate connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_key (may be empty
        for anonymous clusters).
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Weaviate cluster connectivity via /v1/.well-known/ready."""
        if not self.http_client:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="http_client not initialised (install required)",
            )
        try:
            await with_retry(
                lambda: self.http_client.get_ready(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Weaviate cluster ready",
            )
        except WeaviateAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Weaviate auth failed: {exc}",
            )
        except WeaviateNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Weaviate network error: {exc}",
            )
        except WeaviateError as exc:
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
        """Iterate every class, page through objects, normalize, ingest."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        if not self.http_client:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message="http_client not initialised (install required)",
            )

        try:
            schema = await with_retry(
                lambda: self.http_client.list_classes(),
                max_retries=3,
            )
            class_names = [
                c.get("class", "")
                for c in (schema.get("classes") or [])
                if isinstance(c, dict) and c.get("class")
            ]

            for class_name in class_names:
                after: Optional[str] = None
                while True:
                    resp = await with_retry(
                        lambda cn=class_name, a=after: self.http_client.list_objects(
                            class_name=cn,
                            limit=100,
                            after=a,
                        ),
                        max_retries=3,
                    )
                    objs = resp.get("objects") if isinstance(resp, dict) else None
                    if not objs:
                        break

                    last_id: Optional[str] = None
                    for raw in objs:
                        documents_found += 1
                        try:
                            doc = normalize_object(
                                raw,
                                self.connector_id,
                                self.tenant_id,
                                default_class=class_name,
                            )
                            await self.ingest_document(
                                doc,
                                kb_id=kb_id or "",
                                webhook_url=webhook_url,
                            )
                            documents_synced += 1
                            last_id = raw.get("id") if isinstance(raw, dict) else None
                        except Exception as exc:
                            logger.error(
                                "weaviate.sync.object_failed",
                                error=str(exc),
                                class_name=class_name,
                            )
                            documents_failed += 1

                    if len(objs) < 100 or not last_id:
                        break
                    after = last_id

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Weaviate objects",
            )
        except Exception as exc:
            logger.error("weaviate.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    # Cluster

    async def get_meta(self) -> Dict[str, Any]:
        """GET /v1/meta — version + module info."""
        return await with_retry(
            lambda: self.http_client.get_meta(),
            max_retries=3,
        )

    # Schema (classes)

    async def list_classes(self) -> Dict[str, Any]:
        """GET /v1/schema."""
        return await with_retry(
            lambda: self.http_client.list_classes(),
            max_retries=3,
        )

    async def create_class(self, class_body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v1/schema."""
        return await self.http_client.create_class(class_body)

    async def get_class(self, class_name: str) -> Dict[str, Any]:
        """GET /v1/schema/{className}."""
        return await with_retry(
            lambda: self.http_client.get_class(class_name),
            max_retries=3,
        )

    async def delete_class(self, class_name: str) -> Dict[str, Any]:
        """DELETE /v1/schema/{className}."""
        return await self.http_client.delete_class(class_name)

    # Objects

    async def list_objects(
        self,
        *,
        class_name: Optional[str] = None,
        limit: int = 100,
        after: Optional[str] = None,
        include: Optional[str] = None,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/objects."""
        return await with_retry(
            lambda: self.http_client.list_objects(
                class_name=class_name,
                limit=limit,
                after=after,
                include=include,
                tenant=tenant,
            ),
            max_retries=3,
        )

    async def create_object(
        self,
        class_name: str,
        properties: Dict[str, Any],
        *,
        vector: Optional[List[float]] = None,
        object_id: Optional[str] = None,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/objects."""
        return await self.http_client.create_object(
            class_name,
            properties,
            vector=vector,
            object_id=object_id,
            tenant=tenant,
        )

    async def get_object(
        self,
        class_name: str,
        object_id: str,
        *,
        include: Optional[str] = None,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/objects/{className}/{id}."""
        return await with_retry(
            lambda: self.http_client.get_object(
                class_name,
                object_id,
                include=include,
                tenant=tenant,
            ),
            max_retries=3,
        )

    async def update_object(
        self,
        class_name: str,
        object_id: str,
        properties: Dict[str, Any],
        *,
        vector: Optional[List[float]] = None,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PATCH /v1/objects/{className}/{id}."""
        return await self.http_client.update_object(
            class_name,
            object_id,
            properties,
            vector=vector,
            tenant=tenant,
        )

    async def delete_object(
        self,
        class_name: str,
        object_id: str,
        *,
        tenant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """DELETE /v1/objects/{className}/{id}."""
        return await self.http_client.delete_object(
            class_name,
            object_id,
            tenant=tenant,
        )

    # Batch

    async def batch_create_objects(
        self,
        objects: List[Dict[str, Any]],
        *,
        consistency_level: Optional[str] = None,
    ) -> Any:
        """POST /v1/batch/objects."""
        return await self.http_client.batch_create_objects(
            objects,
            consistency_level=consistency_level,
        )

    # GraphQL

    async def graphql_query(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /v1/graphql — Get / Aggregate / Explore."""
        return await with_retry(
            lambda: self.http_client.graphql_query(query, variables=variables),
            max_retries=3,
        )

    # Multi-tenancy

    async def list_tenants(self, class_name: str) -> Any:
        """GET /v1/schema/{className}/tenants."""
        return await with_retry(
            lambda: self.http_client.list_tenants(class_name),
            max_retries=3,
        )

    async def create_tenant(
        self,
        class_name: str,
        tenants: List[Dict[str, Any]],
    ) -> Any:
        """POST /v1/schema/{className}/tenants."""
        return await self.http_client.create_tenants(class_name, tenants)

    async def delete_tenant(
        self,
        class_name: str,
        tenant_names: List[str],
    ) -> Any:
        """DELETE /v1/schema/{className}/tenants."""
        return await self.http_client.delete_tenants(class_name, tenant_names)

    # Backups

    async def create_backup(
        self,
        backend: str,
        backup_id: str,
        *,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /v1/backups/{backend}."""
        return await self.http_client.create_backup(
            backend,
            backup_id,
            include=include,
            exclude=exclude,
        )

    async def get_backup_status(
        self,
        backend: str,
        backup_id: str,
    ) -> Dict[str, Any]:
        """GET /v1/backups/{backend}/{id}."""
        return await with_retry(
            lambda: self.http_client.get_backup_status(backend, backup_id),
            max_retries=3,
        )
