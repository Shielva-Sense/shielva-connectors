"""Elasticsearch connector — orchestration only.

All HTTP calls   → client/http_client.py
All normalization → helpers/normalizer.py
All utilities    → helpers/utils.py
All exceptions   → exceptions.py

Auth: API key (preferred) — `Authorization: ApiKey <base64(id:api_key)>` — or
HTTP Basic — `Authorization: Basic <base64(u:p)>`. Anonymous self-hosted
clusters are also supported (both credential surfaces left blank).

Per-tenant cluster URL: every tenant brings their own `base_url` (Elastic
Cloud `https://*.cloud.es.io:9243` or self-hosted). The connector reads
`self.config["base_url"]` (alias accepted: `host`) and never embeds a default.
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

from client.http_client import ElasticsearchHTTPClient
from exceptions import (
    ElasticsearchAuthError,
    ElasticsearchError,
    ElasticsearchNetworkError,
    ElasticsearchNotFound,
    ElasticsearchRateLimitError,
)
from helpers.normalizer import normalize_index
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)


class ElasticsearchConnector(BaseConnector):
    """Shielva connector for Elasticsearch (self-hosted or Elastic Cloud)."""

    CONNECTOR_TYPE = "elasticsearch"
    CONNECTOR_NAME = "Elasticsearch"
    AUTH_TYPE = "api_key"

    # Per task spec: only base_url is mandatory. api_key is optional —
    # anonymous self-hosted clusters and HTTP Basic both supported.
    REQUIRED_CONFIG_KEYS: List[str] = ["base_url"]

    OPTIONAL_CONFIG_KEYS: List[str] = [
        "api_key",
        "username",
        "password",
        "verify_ssl",
        "rate_limit_per_min",
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
        # `base_url` is canonical; `host` is back-compat alias for older
        # installs that pre-date the rename.
        base_url_raw = self.config.get("base_url") or self.config.get("host") or ""
        self.base_url: str = base_url_raw.rstrip("/")
        # Keep `host` as a property-shim for the few callers that read it.
        self.host: str = self.base_url

        self.api_key: str = self.config.get("api_key", "") or ""
        self.username: str = self.config.get("username", "") or ""
        self.password: str = self.config.get("password", "") or ""

        verify = self.config.get("verify_ssl", True)
        if isinstance(verify, str):
            verify = verify.strip().lower() not in ("false", "0", "no")
        self.verify_ssl: bool = bool(verify)

        self.rate_limit_per_min: int = int(
            self.config.get("rate_limit_per_min", 600) or 600
        )

        # Build the HTTP client lazily — install() runs before credentials
        # are present in some flows. If base_url is present, we can build
        # immediately (anonymous mode is valid).
        self.http_client: Optional[ElasticsearchHTTPClient]
        if self.base_url:
            self.http_client = ElasticsearchHTTPClient(
                base_url=self.base_url,
                api_key=self.api_key or None,
                username=self.username or None,
                password=self.password or None,
                verify_ssl=self.verify_ssl,
            )
        else:
            self.http_client = None

    # ── Internal helpers ────────────────────────────────────────────────────

    def _ensure_client(self) -> ElasticsearchHTTPClient:
        if self.http_client is None:
            raise ElasticsearchAuthError(
                "Elasticsearch base_url not configured — re-run install()"
            )
        return self.http_client

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and verify reachability."""
        if not self.base_url:
            logger.warning(
                "elasticsearch.install.missing_base_url",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="base_url is required",
            )

        # Construct the client now that base_url is known.
        if self.http_client is None:
            self.http_client = ElasticsearchHTTPClient(
                base_url=self.base_url,
                api_key=self.api_key or None,
                username=self.username or None,
                password=self.password or None,
                verify_ssl=self.verify_ssl,
            )

        try:
            await self.http_client.info()
            await self.save_config(
                {
                    "base_url": self.base_url,
                    "api_key": self.api_key,
                    "username": self.username,
                    "password": self.password,
                    "verify_ssl": self.verify_ssl,
                    "rate_limit_per_min": self.rate_limit_per_min,
                }
            )
            logger.info(
                "elasticsearch.install.ok",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Elasticsearch cluster reachable — connector ready",
            )
        except ElasticsearchAuthError as exc:
            logger.warning(
                "elasticsearch.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="API key / basic-auth credentials rejected by Elasticsearch",
            )
        except (ElasticsearchNetworkError, ElasticsearchError) as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.PENDING,
                message=f"Elasticsearch unreachable: {exc}",
            )

    async def authorize(
        self, auth_code: str = "", state: str = "",
    ) -> TokenInfo:
        """API-key auth — no OAuth code exchange.

        Returned for ABI compatibility: a TokenInfo whose access_token is the
        configured api_key (or empty for basic/anonymous).
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Probe cluster connectivity via `GET /_cluster/health`."""
        if self.http_client is None:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing base_url or credentials",
            )
        try:
            payload = await with_retry(
                lambda: self.http_client.cluster_health(level="cluster"),
                max_retries=2,
            )
            status = (payload or {}).get("status", "").lower()
            # green/yellow → reachable. red is still reachable but degraded.
            if status == "red":
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,
                    auth_status=AuthStatus.CONNECTED,
                    message="Cluster health: red",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Elasticsearch cluster reachable ({status or 'unknown'})",
            )
        except ElasticsearchAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Credentials rejected: {exc}",
            )
        except ElasticsearchRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Rate limited: {exc}",
            )
        except (ElasticsearchNetworkError, ElasticsearchError) as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
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
        """Sync Elasticsearch index inventory into the Shielva KB.

        Elasticsearch is a data store, not a content source. `sync()` therefore
        enumerates indices and ingests one NormalizedDocument per index — an
        inventory view for the tenant. Real ingestion *into* Elasticsearch is
        driven via `index_document()` / `bulk()` from other connectors.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        if self.http_client is None:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message="Missing base_url or credentials",
            )
        try:
            items = await with_retry(
                lambda: self.http_client.cat_indices(
                    index_pattern="*", format="json",
                ),
                max_retries=3,
            )
            rows = items if isinstance(items, list) else []
            for raw in rows:
                documents_found += 1
                try:
                    doc = normalize_index(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "elasticsearch.sync.index_failed",
                        error=str(exc),
                        index=raw.get("index") if isinstance(raw, dict) else None,
                    )
                    documents_failed += 1

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED
                    if documents_failed == 0
                    else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Enumerated {documents_synced}/{documents_found} indices",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "elasticsearch.sync.failed",
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

    # ── User-requested standalone methods (OCP) ─────────────────────────────

    async def get_cluster_health(self, level: str = "cluster") -> Dict[str, Any]:
        """GET /_cluster/health — cluster health summary at *level*.

        Canonical name for the user-facing method; `cluster_health()` is kept
        as a back-compat shim.
        """
        client = self._ensure_client()
        return await with_retry(
            lambda: client.cluster_health(level=level),
            max_retries=3,
        )

    async def cluster_health(self, level: str = "cluster") -> Dict[str, Any]:
        """Back-compat shim → :py:meth:`get_cluster_health`."""
        return await self.get_cluster_health(level=level)

    async def list_indices(
        self, index_pattern: str = "*", format: str = "json",
    ) -> Any:
        """GET /_cat/indices/{pattern}?format=… — list indices."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.cat_indices(
                index_pattern=index_pattern, format=format,
            ),
            max_retries=3,
        )

    async def get_index(self, index: str) -> Dict[str, Any]:
        """GET /{index} — full index settings + mappings + aliases."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.get_index(index=index),
            max_retries=3,
        )

    async def create_index(
        self,
        index: str,
        settings: Optional[Dict[str, Any]] = None,
        mappings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """PUT /{index} — create index with optional settings + mappings."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.create_index(
                index=index, settings=settings, mappings=mappings,
            ),
            max_retries=3,
        )

    async def delete_index(self, index: str) -> Dict[str, Any]:
        """DELETE /{index}."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.delete_index(index=index),
            max_retries=3,
        )

    async def index_document(
        self,
        index: str,
        document: Dict[str, Any],
        doc_id: Optional[str] = None,
        refresh: str = "false",
    ) -> Dict[str, Any]:
        """POST /{index}/_doc (auto-id) or PUT /{index}/_doc/{id} (upsert)."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.index_document(
                index=index, document=document, doc_id=doc_id, refresh=refresh,
            ),
            max_retries=3,
        )

    async def get_document(self, index: str, doc_id: str) -> Dict[str, Any]:
        """GET /{index}/_doc/{id}."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.get_document(index=index, doc_id=doc_id),
            max_retries=3,
        )

    async def update_document(
        self,
        index: str,
        doc_id: str,
        doc: Dict[str, Any],
        doc_as_upsert: bool = False,
    ) -> Dict[str, Any]:
        """POST /{index}/_update/{id}."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.update_document(
                index=index, doc_id=doc_id, doc=doc, doc_as_upsert=doc_as_upsert,
            ),
            max_retries=3,
        )

    async def delete_document(
        self, index: str, doc_id: str,
    ) -> Dict[str, Any]:
        """DELETE /{index}/_doc/{id}."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.delete_document(index=index, doc_id=doc_id),
            max_retries=3,
        )

    async def search(
        self,
        index: str,
        query: Dict[str, Any],
        size: int = 10,
        from_: int = 0,
        sort: Optional[List[Any]] = None,
        aggs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /{index}/_search."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.search(
                index=index, query=query, size=size, from_=from_,
                sort=sort, aggs=aggs,
            ),
            max_retries=3,
        )

    async def bulk(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /_bulk — NDJSON bulk operations.

        *operations* is the paired action+source list (e.g.
        `[{"index": {"_index": "x"}}, {"field": "v"}]`); the client serialises
        to NDJSON and sets Content-Type=application/x-ndjson.
        """
        client = self._ensure_client()
        return await with_retry(
            lambda: client.bulk(operations=operations),
            max_retries=3,
        )

    async def count(
        self, index: str, query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /{index}/_count."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.count(index=index, query=query),
            max_retries=3,
        )

    async def get_mapping(self, index: str) -> Dict[str, Any]:
        """GET /{index}/_mapping."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.get_mapping(index=index),
            max_retries=3,
        )

    async def put_mapping(
        self, index: str, properties: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /{index}/_mapping."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.put_mapping(index=index, properties=properties),
            max_retries=3,
        )

    async def list_aliases(self, name: str = "*") -> Any:
        """GET /_cat/aliases[/{name}]?format=json — list aliases."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.cat_aliases(name=name, format="json"),
            max_retries=3,
        )

    async def list_snapshots(self, repository: str) -> Dict[str, Any]:
        """GET /_snapshot/{repository}/_all — list snapshots in a repository."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.list_snapshots(repository=repository),
            max_retries=3,
        )
