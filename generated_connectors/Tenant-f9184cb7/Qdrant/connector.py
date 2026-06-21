"""Qdrant connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: API key (Qdrant Cloud). The key is sent in the lowercase `api-key`
header — NOT the `Authorization` header and NOT prefixed with `Bearer`.
For default self-hosted deployments with no auth, `api_key` may be empty
and the header is omitted entirely.

Required headers:
    api-key:      <api_key>          (when set)
    Content-Type: application/json
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

from client.http_client import QdrantHTTPClient
from exceptions import (
    QdrantAuthError,
    QdrantError,
    QdrantNetworkError,
    QdrantNotFound,
)
from helpers.normalizer import normalize_collection
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_DEFAULT_RATE_LIMIT_PER_MIN = 600


class QdrantConnector(BaseConnector):
    """Shielva connector for the Qdrant vector database REST API.

    Wraps Collections (CRUD), Points (upsert/get/delete/search/scroll/count),
    Indexes (payload indexes), Snapshots, Cluster, and Service (health,
    telemetry) surfaces. Cluster URL is per-tenant (Qdrant Cloud) or
    per-deployment (self-hosted) — there is no provider-wide default.
    """

    CONNECTOR_TYPE = "qdrant"
    CONNECTOR_NAME = "Qdrant"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "base_url",
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
        self.api_key: str = self.config.get("api_key", "") or ""
        self.base_url: str = self.config.get("base_url", "") or ""
        self.default_collection: str = self.config.get("default_collection", "") or ""
        try:
            self.rate_limit_per_min: int = int(
                self.config.get("rate_limit_per_min") or _DEFAULT_RATE_LIMIT_PER_MIN
            )
        except (TypeError, ValueError):
            self.rate_limit_per_min = _DEFAULT_RATE_LIMIT_PER_MIN

        self.http_client = QdrantHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Qdrant api-key install requires `base_url`. `api_key` is conditionally
        required — Cloud demands it; default self-hosted may not. We accept
        both paths and let `health_check()` confirm reachability.
        """
        base_url = self.config.get("base_url", "")
        if not base_url:
            logger.warning(
                "qdrant.install.missing_base_url",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="base_url is required",
            )

        await self.save_config(
            {
                "base_url": base_url,
                "api_key": self.config.get("api_key", ""),
                "default_collection": self.config.get("default_collection", ""),
                "rate_limit_per_min": self.config.get(
                    "rate_limit_per_min", _DEFAULT_RATE_LIMIT_PER_MIN
                ),
            }
        )
        logger.info("qdrant.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Qdrant connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Qdrant cluster reachability.

        Probe order: `/healthz` → `/readyz` → `/`. The first one that
        succeeds wins. `/healthz` is the canonical liveness endpoint;
        `/readyz` is the readiness probe; `/` is the version banner.
        """
        try:
            await with_retry(
                lambda: self.http_client.healthz(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Qdrant cluster reachable",
            )
        except QdrantAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Qdrant auth failed: {exc}",
            )
        except QdrantNotFound:
            # Older self-hosted builds don't ship /healthz — fall back to /.
            try:
                await with_retry(lambda: self.http_client.root(), max_retries=2)
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.HEALTHY,
                    auth_status=AuthStatus.CONNECTED,
                    message="Qdrant cluster reachable (root probe)",
                )
            except QdrantError as exc:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,
                    auth_status=AuthStatus.CONNECTED,
                    message=f"Qdrant unreachable: {exc}",
                )
        except QdrantNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Qdrant network error: {exc}",
            )
        except QdrantError as exc:
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
        """Mirror the cluster's collection catalogue into the Shielva KB.

        Qdrant is a vector store — it does not hold "documents" in the
        knowledge-base sense. Sync therefore enumerates collections and
        emits one NormalizedDocument per collection so the Shielva KB can
        render the cluster's catalogue (vector size, distance, points count).
        Operational vector ops happen via upsert_points / search_points.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            list_resp = await with_retry(
                lambda: self.http_client.list_collections(),
                max_retries=3,
            )
            # Qdrant wraps results: `{"result": {"collections": [{"name": "x"}]}, ...}`.
            result = list_resp.get("result", list_resp) if isinstance(list_resp, dict) else {}
            collections = result.get("collections", []) if isinstance(result, dict) else []

            for entry in collections or []:
                name = entry.get("name", "") if isinstance(entry, dict) else ""
                if not name:
                    continue
                documents_found += 1
                try:
                    detail = await with_retry(
                        lambda n=name: self.http_client.get_collection(n),
                        max_retries=3,
                    )
                    doc = normalize_collection(
                        detail,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        collection_name=name,
                    )
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error("qdrant.sync.collection_failed", name=name, error=str(exc))
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Qdrant collections",
            )
        except Exception as exc:
            logger.error("qdrant.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Collections ────────────────────────────────────────────────────────

    async def list_collections(self) -> Dict[str, Any]:
        """GET /collections."""
        return await with_retry(
            lambda: self.http_client.list_collections(),
            max_retries=3,
        )

    async def create_collection(
        self,
        collection_name: str,
        vectors: Dict[str, Any],
        optimizers_config: Optional[Dict[str, Any]] = None,
        shard_number: int = 1,
        replication_factor: int = 1,
        write_consistency_factor: int = 1,
        on_disk_payload: bool = False,
    ) -> Dict[str, Any]:
        """PUT /collections/{name} — create a new collection."""
        return await self.http_client.create_collection(
            collection_name=collection_name,
            vectors=vectors,
            optimizers_config=optimizers_config,
            shard_number=shard_number,
            replication_factor=replication_factor,
            write_consistency_factor=write_consistency_factor,
            on_disk_payload=on_disk_payload,
        )

    async def get_collection(self, collection_name: str) -> Dict[str, Any]:
        """GET /collections/{name}."""
        return await with_retry(
            lambda: self.http_client.get_collection(collection_name),
            max_retries=3,
        )

    async def delete_collection(self, collection_name: str) -> Dict[str, Any]:
        """DELETE /collections/{name}."""
        return await self.http_client.delete_collection(collection_name)

    async def update_collection(
        self,
        collection_name: str,
        optimizers_config: Optional[Dict[str, Any]] = None,
        vectors_config: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """PATCH /collections/{name}."""
        return await self.http_client.update_collection(
            collection_name=collection_name,
            optimizers_config=optimizers_config,
            vectors_config=vectors_config,
            params=params,
        )

    # ── Points (write) ─────────────────────────────────────────────────────

    async def upsert_points(
        self,
        collection_name: str,
        points: List[Dict[str, Any]],
        wait: bool = True,
    ) -> Dict[str, Any]:
        """PUT /collections/{name}/points — upsert a batch of points."""
        return await self.http_client.upsert_points(
            collection_name=collection_name,
            points=points,
            wait=wait,
        )

    async def delete_points(
        self,
        collection_name: str,
        points: Optional[List[Any]] = None,
        filter: Optional[Dict[str, Any]] = None,
        wait: bool = True,
    ) -> Dict[str, Any]:
        """POST /collections/{name}/points/delete — by id list or by filter."""
        return await self.http_client.delete_points(
            collection_name=collection_name,
            points=points,
            filter=filter,
            wait=wait,
        )

    # ── Points (read) ──────────────────────────────────────────────────────

    async def get_points(
        self,
        collection_name: str,
        ids: List[Any],
        with_payload: bool = True,
        with_vector: bool = False,
    ) -> Dict[str, Any]:
        """POST /collections/{name}/points — retrieve by id list."""
        return await with_retry(
            lambda: self.http_client.get_points(
                collection_name=collection_name,
                ids=ids,
                with_payload=with_payload,
                with_vector=with_vector,
            ),
            max_retries=3,
        )

    async def search_points(
        self,
        collection_name: str,
        vector: List[float],
        limit: int = 10,
        with_payload: bool = True,
        with_vector: bool = False,
        score_threshold: Optional[float] = None,
        filter: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /collections/{name}/points/search — vector similarity search."""
        return await with_retry(
            lambda: self.http_client.search_points(
                collection_name=collection_name,
                vector=vector,
                limit=limit,
                with_payload=with_payload,
                with_vector=with_vector,
                score_threshold=score_threshold,
                filter=filter,
                params=params,
            ),
            max_retries=3,
        )

    async def scroll_points(
        self,
        collection_name: str,
        limit: int = 100,
        offset: Optional[Any] = None,
        with_payload: bool = True,
        with_vector: bool = False,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /collections/{name}/points/scroll — cursor-paged points."""
        return await with_retry(
            lambda: self.http_client.scroll_points(
                collection_name=collection_name,
                limit=limit,
                offset=offset,
                with_payload=with_payload,
                with_vector=with_vector,
                filter=filter,
            ),
            max_retries=3,
        )

    async def count_points(
        self,
        collection_name: str,
        filter: Optional[Dict[str, Any]] = None,
        exact: bool = True,
    ) -> Dict[str, Any]:
        """POST /collections/{name}/points/count."""
        return await with_retry(
            lambda: self.http_client.count_points(
                collection_name=collection_name,
                filter=filter,
                exact=exact,
            ),
            max_retries=3,
        )

    # ── Indexes ────────────────────────────────────────────────────────────

    async def create_payload_index(
        self,
        collection_name: str,
        field_name: str,
        field_schema: str,
        wait: bool = True,
    ) -> Dict[str, Any]:
        """PUT /collections/{name}/index — create a payload-field index."""
        return await self.http_client.create_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=field_schema,
            wait=wait,
        )

    async def delete_payload_index(
        self,
        collection_name: str,
        field_name: str,
        wait: bool = True,
    ) -> Dict[str, Any]:
        """DELETE /collections/{name}/index/{field_name}."""
        return await self.http_client.delete_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            wait=wait,
        )

    # ── Snapshots ──────────────────────────────────────────────────────────

    async def list_snapshots(self, collection_name: str) -> Dict[str, Any]:
        """GET /collections/{name}/snapshots."""
        return await with_retry(
            lambda: self.http_client.list_snapshots(collection_name),
            max_retries=3,
        )

    async def create_snapshot(self, collection_name: str) -> Dict[str, Any]:
        """POST /collections/{name}/snapshots."""
        return await self.http_client.create_snapshot(collection_name)

    # ── Cluster / service ──────────────────────────────────────────────────

    async def get_cluster_info(self) -> Dict[str, Any]:
        """GET /cluster — return cluster topology and peer info."""
        return await with_retry(
            lambda: self.http_client.get_cluster_info(),
            max_retries=3,
        )
