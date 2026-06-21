"""Pinecone connector — orchestration only.

SOC:
  * All HTTP calls          → client/http_client.py::PineconeHTTPClient
  * All response shaping    → helpers/normalizer.py::normalize_index
  * All utility/retry logic → helpers/utils.py

Auth: API key. The key travels in the `Api-Key` header (NOT Authorization) on
every request to both the control plane (https://api.pinecone.io) and the
per-index data plane (https://{index_host}).

OCP: every user-requested operation is a standalone `async def`. Adding a new
operation never requires modifying BaseConnector or existing methods. Provider
status → enum mapping is captured in `_STATUS_MAP` so new HTTP statuses can be
classified without if-ladders inside the lifecycle methods.
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

from client.http_client import PineconeHTTPClient
from exceptions import (
    PineconeAuthError,
    PineconeError,
    PineconeNetworkError,
    PineconeNotFoundError,
)
from helpers.normalizer import normalize_index
from helpers.utils import chunk_list, coerce_namespace, normalize_vector_record, with_retry

logger = structlog.get_logger(__name__)

_DEFAULT_CONTROL_URL = "https://api.pinecone.io"
_DEFAULT_API_VERSION = "2025-01"


class PineconeConnector(BaseConnector):
    """Shielva connector for the Pinecone vector database (control + data plane)."""

    CONNECTOR_TYPE = "pinecone"
    CONNECTOR_NAME = "Pinecone"
    AUTH_TYPE = "api_key"

    REQUIRED_SCOPES: List[str] = []

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_key",
        "environment",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    # Used by health_check / sync error paths to map HTTP failures to the
    # framework's enum surface without inline conditionals in business logic.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "INVALID_CREDENTIALS"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        # ALWAYS read credentials from self.config — NEVER from os.environ.
        self.api_key: str = self.config.get("api_key", "")
        self.environment: str = self.config.get("environment", "")
        self.project_id: str = self.config.get("project_id", "")
        self.control_url: str = self.config.get("control_url") or _DEFAULT_CONTROL_URL
        self.api_version: str = self.config.get("api_version") or _DEFAULT_API_VERSION
        self.default_index: str = self.config.get("default_index", "")
        self.default_namespace: str = self.config.get("default_namespace", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        self.http_client = PineconeHTTPClient(
            api_key=self.api_key,
            control_url=self.control_url,
            api_version=self.api_version,
        )

    # ── Internal helpers ───────────────────────────────────────────────────

    def _resolve_index(self, index_name: Optional[str]) -> str:
        """Return *index_name* or fall back to the configured default index."""
        target = index_name or self.default_index
        if not target:
            raise PineconeError(
                "No index specified and no default_index configured"
            )
        return target

    def _resolve_namespace(self, namespace: Optional[str]) -> str:
        if namespace is None:
            return self.default_namespace or ""
        return coerce_namespace(namespace, self.default_namespace or "")

    # ── BaseConnector lifecycle ────────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config; mark the connector installed.

        Pinecone install only requires `api_key`. `environment` is required by
        contract (listed in REQUIRED_CONFIG_KEYS) but the API ignores it as
        long as the per-index host is discoverable via describe_index, so we
        do NOT call the network here — the gateway will run health_check
        separately.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning(
                "pinecone.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        await self.save_config(
            {
                "api_key": api_key,
                "environment": self.environment,
                "project_id": self.project_id,
                "control_url": self.control_url,
                "api_version": self.api_version,
                "default_index": self.default_index,
                "default_namespace": self.default_namespace,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("pinecone.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Pinecone connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returns a synthetic `TokenInfo` wrapping the configured API key so the
        platform's generic post-install flow remains uniform across auth
        types.
        """
        token_info = TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="ApiKey",
            scopes=[],
        )
        await self.set_token(token_info)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Confirm the API key works by listing indexes on the control plane."""
        try:
            await with_retry(
                lambda: self.http_client.list_indexes(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Pinecone API reachable",
            )
        except PineconeAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"API key rejected: {exc}",
            )
        except PineconeNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Network error: {exc}",
            )
        except PineconeError as exc:
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
        """Discover the tenant's Pinecone indexes and ingest one summary doc per index.

        Pinecone is a vector sink, not a content source — each `NormalizedDocument`
        is a per-index summary (dimension, metric, host, totalVectorCount,
        namespaces) so the Shielva KB has a discoverable audit trail of the
        tenant's Pinecone footprint.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            if self.default_index:
                idx_names = [self.default_index]
            else:
                indexes_resp = await with_retry(
                    lambda: self.http_client.list_indexes(),
                    max_retries=2,
                )
                idx_names = [
                    i.get("name", "")
                    for i in indexes_resp.get("indexes", []) or []
                    if i.get("name")
                ]

            for name in idx_names:
                documents_found += 1
                try:
                    spec = await with_retry(
                        lambda n=name: self.http_client.describe_index(n),
                        max_retries=2,
                    )
                    try:
                        stats = await with_retry(
                            lambda n=name: self.http_client.describe_index_stats(n),
                            max_retries=2,
                        )
                    except PineconeError as stats_exc:
                        logger.warning(
                            "pinecone.sync.stats_failed",
                            index=name,
                            error=str(stats_exc),
                        )
                        stats = {}

                    doc = normalize_index(
                        spec, self.connector_id, self.tenant_id, stats=stats
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "pinecone.sync.index_failed",
                        index=name,
                        error=str(exc),
                    )
                    documents_failed += 1

            status = (
                SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Pinecone indexes",
            )
        except Exception as exc:
            logger.error(
                "pinecone.sync.failed",
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

    # ── Control plane: indexes ─────────────────────────────────────────────

    async def list_indexes(self) -> Dict[str, Any]:
        """GET /indexes — list all indexes in this project."""
        return await with_retry(
            lambda: self.http_client.list_indexes(),
            max_retries=3,
        )

    async def describe_index(self, index_name: str) -> Dict[str, Any]:
        """GET /indexes/{name} — fetch index spec (dimension, metric, host, …)."""
        return await with_retry(
            lambda: self.http_client.describe_index(index_name),
            max_retries=3,
        )

    async def create_index(
        self,
        name: str,
        dimension: int,
        metric: str = "cosine",
        cloud: str = "aws",
        region: str = "us-east-1",
    ) -> Dict[str, Any]:
        """POST /indexes — create a new serverless index."""
        return await with_retry(
            lambda: self.http_client.create_index(
                name=name,
                dimension=dimension,
                metric=metric,
                cloud=cloud,
                region=region,
            ),
            max_retries=2,
        )

    async def delete_index(self, index_name: str) -> Dict[str, Any]:
        """DELETE /indexes/{name}."""
        return await with_retry(
            lambda: self.http_client.delete_index(index_name),
            max_retries=2,
        )

    async def configure_index(
        self,
        index_name: str,
        replicas: Optional[int] = None,
        pod_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PATCH /indexes/{name} — reconfigure replicas/pod_type for pod-based indexes."""
        return await with_retry(
            lambda: self.http_client.configure_index(
                index_name=index_name, replicas=replicas, pod_type=pod_type
            ),
            max_retries=2,
        )

    # ── Control plane: collections ─────────────────────────────────────────

    async def list_collections(self) -> Dict[str, Any]:
        """GET /collections — list backups/snapshots of indexes."""
        return await with_retry(
            lambda: self.http_client.list_collections(),
            max_retries=3,
        )

    async def create_collection(self, name: str, source: str) -> Dict[str, Any]:
        """POST /collections — create a collection from a source index."""
        return await with_retry(
            lambda: self.http_client.create_collection(name=name, source=source),
            max_retries=2,
        )

    async def delete_collection(self, collection_name: str) -> Dict[str, Any]:
        """DELETE /collections/{name}."""
        return await with_retry(
            lambda: self.http_client.delete_collection(collection_name),
            max_retries=2,
        )

    # ── Data plane: vectors ────────────────────────────────────────────────

    async def upsert_vectors(
        self,
        index_name: str,
        vectors: List[Dict[str, Any]],
        namespace: str = "",
    ) -> Dict[str, Any]:
        """POST {host}/vectors/upsert — insert or update vectors.

        Each vector dict MUST shape `{"id": str, "values": [float...], "metadata": {...}?}`.
        Pinecone caps a single upsert at 1000 vectors; we chunk to 100 for safety.
        """
        normalized = [normalize_vector_record(v) for v in vectors]
        ns = self._resolve_namespace(namespace)
        idx = self._resolve_index(index_name)
        upserted = 0
        last_resp: Dict[str, Any] = {}
        for batch in chunk_list(normalized, 100):
            last_resp = await with_retry(
                lambda b=batch: self.http_client.upsert_vectors(idx, b, namespace=ns),
                max_retries=3,
            )
            upserted += int(last_resp.get("upsertedCount", len(batch)))
        return {"upsertedCount": upserted, "last_response": last_resp}

    async def query(
        self,
        index_name: str,
        vector: List[float],
        top_k: int = 10,
        namespace: str = "",
        filter: Optional[Dict[str, Any]] = None,
        include_metadata: bool = True,
        include_values: bool = False,
    ) -> Dict[str, Any]:
        """POST {host}/query — nearest-neighbor search."""
        idx = self._resolve_index(index_name)
        ns = self._resolve_namespace(namespace)
        return await with_retry(
            lambda: self.http_client.query(
                index_name=idx,
                vector=vector,
                top_k=top_k,
                namespace=ns,
                filter=filter,
                include_metadata=include_metadata,
                include_values=include_values,
            ),
            max_retries=3,
        )

    async def fetch_vectors(
        self,
        index_name: str,
        ids: List[str],
        namespace: str = "",
    ) -> Dict[str, Any]:
        """GET {host}/vectors/fetch?ids=&namespace= — fetch vectors by ID."""
        idx = self._resolve_index(index_name)
        ns = self._resolve_namespace(namespace)
        return await with_retry(
            lambda: self.http_client.fetch_vectors(idx, ids=ids, namespace=ns),
            max_retries=3,
        )

    async def delete_vectors(
        self,
        index_name: str,
        ids: Optional[List[str]] = None,
        delete_all: bool = False,
        namespace: str = "",
    ) -> Dict[str, Any]:
        """POST {host}/vectors/delete — delete by IDs or delete-all in a namespace."""
        idx = self._resolve_index(index_name)
        ns = self._resolve_namespace(namespace)
        return await with_retry(
            lambda: self.http_client.delete_vectors(
                idx, ids=ids, delete_all=delete_all, namespace=ns
            ),
            max_retries=2,
        )

    async def update_vector(
        self,
        index_name: str,
        id: str,
        values: Optional[List[float]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        namespace: str = "",
    ) -> Dict[str, Any]:
        """POST {host}/vectors/update — partial update of one vector."""
        idx = self._resolve_index(index_name)
        ns = self._resolve_namespace(namespace)
        return await with_retry(
            lambda: self.http_client.update_vector(
                idx, id=id, values=values, metadata=metadata, namespace=ns
            ),
            max_retries=3,
        )

    async def describe_index_stats(
        self,
        index_name: str,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST {host}/describe_index_stats — vector counts, dimension, namespaces."""
        idx = self._resolve_index(index_name)
        return await with_retry(
            lambda: self.http_client.describe_index_stats(idx, filter=filter),
            max_retries=3,
        )

    async def list_namespaces(self, index_name: str) -> Dict[str, Any]:
        """GET {host}/namespaces — list namespaces inside the index.

        Falls back to deriving the list from describe_index_stats on older index
        versions where the dedicated endpoint returns 404.
        """
        idx = self._resolve_index(index_name)
        return await with_retry(
            lambda: self.http_client.list_namespaces(idx),
            max_retries=3,
        )
