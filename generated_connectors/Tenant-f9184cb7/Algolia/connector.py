"""Algolia connector — orchestration only.

All HTTP calls   → client/http_client.py
All normalization → helpers/normalizer.py
All utilities    → helpers/utils.py
All exceptions   → exceptions.py

Auth: API key. The Application ID + API Key are sent on every request as
the ``X-Algolia-Application-Id`` and ``X-Algolia-API-Key`` headers — never
as query parameters.

DSN routing: reads go through ``<app_id>-dsn.algolia.net`` (geo-DNS to the
lowest-latency PoP); writes go through ``<app_id>.algolia.net`` (single
write primary). A separate-zone ``algolianet.com`` fallback ring catches
in-flight outages. See ``helpers/utils.py::build_read_hosts`` for details.
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

from client.http_client import AlgoliaHTTPClient
from exceptions import (
    AlgoliaAuthError,
    AlgoliaError,
    AlgoliaNetworkError,
    AlgoliaNotFound,
    AlgoliaRateLimitError,
)
from helpers.normalizer import normalize_index, normalize_object
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)


class AlgoliaConnector(BaseConnector):
    """Shielva connector for Algolia (search-as-a-service).

    Public surface — every method below maps 1:1 to an Algolia REST endpoint
    (or to the lifecycle contract enforced by ``BaseConnector``). The
    connector itself contains no raw HTTP — that lives in
    ``client/http_client.py::AlgoliaHTTPClient``.
    """

    CONNECTOR_TYPE = "algolia"
    CONNECTOR_NAME = "Algolia"
    AUTH_TYPE = "api_key"

    # Public required-config keys (Algolia uses Application ID + API Key).
    REQUIRED_CONFIG_KEYS: List[str] = ["app_id", "api_key"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "INVALID_CREDENTIALS"),
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
        self.app_id: str = self.config.get("app_id", "")
        self.api_key: str = self.config.get("api_key", "")
        self.default_index: str = self.config.get("default_index", "")
        self.timeout_s: float = float(self.config.get("timeout_s", 30) or 30)

        # http_client is only constructable when both credentials are present.
        # install() catches the MISSING_CREDENTIALS case before we get here in
        # the normal flow; bare instantiation with empty config still works.
        if self.app_id and self.api_key:
            self.http_client: Optional[AlgoliaHTTPClient] = AlgoliaHTTPClient(
                app_id=self.app_id,
                api_key=self.api_key,
                timeout_s=self.timeout_s,
            )
        else:
            self.http_client = None

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate config and probe Algolia to verify the key.

        Returns ``ConnectorStatus`` with the resulting health + auth status.
        Does NOT raise — every failure path maps to a typed status so the
        gateway can render a stable error UI.
        """
        if not self.app_id or not self.api_key:
            logger.warning(
                "algolia.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="app_id and api_key are required",
            )

        try:
            if self.http_client is None:
                self.http_client = AlgoliaHTTPClient(
                    app_id=self.app_id,
                    api_key=self.api_key,
                    timeout_s=self.timeout_s,
                )
            await self.http_client.list_indexes()
            await self.save_config(
                {
                    "app_id": self.app_id,
                    "api_key": self.api_key,
                    "default_index": self.default_index,
                    "timeout_s": self.timeout_s,
                }
            )
            logger.info("algolia.install.ok", connector_id=self.connector_id)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Algolia API reachable — connector ready",
            )
        except AlgoliaAuthError as exc:
            logger.warning(
                "algolia.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="app_id or api_key rejected by Algolia",
            )
        except (AlgoliaNetworkError, AlgoliaError) as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.PENDING,
                message=f"Algolia unreachable: {exc}",
            )

    async def authorize(
        self, auth_code: str = "", state: str = ""
    ) -> TokenInfo:
        """API-key flow has no OAuth code-exchange.

        Returned for surface compatibility with ``BaseConnector``: a
        ``TokenInfo`` whose ``access_token`` is the configured ``api_key``.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Probe ``GET /1/indexes`` as a lightweight liveness check."""
        if self.http_client is None:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing app_id or api_key",
            )
        try:
            await self.http_client.list_indexes()
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Algolia API reachable",
            )
        except AlgoliaAuthError as exc:
            health, auth = self._STATUS_MAP.get(
                exc.status_code, ("DEGRADED", "INVALID_CREDENTIALS")
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth[health],
                auth_status=AuthStatus[auth],
                message=f"Algolia auth failed: {exc}",
            )
        except AlgoliaRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Algolia rate-limited: {exc}",
            )
        except AlgoliaNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Algolia network error: {exc}",
            )
        except AlgoliaError as exc:
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
        """Enumerate indexes and (when *full*) browse their objects into the KB.

        Algolia is most often used as a **write target** (you push records to
        it), so the default sync is an inventory pass: it ingests one
        ``NormalizedDocument`` per index with metadata about size + last build.
        When *full=True*, the connector additionally browses every index and
        ingests each object as its own ``NormalizedDocument``.
        """
        if self.http_client is None:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message="Missing app_id or api_key",
            )

        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            indexes_resp = await with_retry(
                lambda: self.http_client.list_indexes(),
                max_retries=3,
            )
            items = indexes_resp.get("items", []) if isinstance(indexes_resp, dict) else []

            for raw_index in items:
                documents_found += 1
                try:
                    doc = normalize_index(
                        raw_index,
                        tenant_id=self.tenant_id,
                        connector_id=self.connector_id,
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:  # noqa: BLE001 — last-resort guard
                    logger.error(
                        "algolia.sync.index_failed",
                        error=str(exc),
                        index=raw_index.get("name"),
                    )
                    documents_failed += 1

                if full:
                    index_name = raw_index.get("name", "")
                    if not index_name:
                        continue
                    cursor: Optional[str] = None
                    while True:
                        try:
                            browse_resp = await with_retry(
                                lambda idx=index_name, cur=cursor: self.http_client.browse_index(
                                    idx, cursor=cur
                                ),
                                max_retries=3,
                            )
                        except AlgoliaError as exc:
                            logger.error(
                                "algolia.sync.browse_failed",
                                error=str(exc),
                                index=index_name,
                            )
                            documents_failed += 1
                            break
                        hits = browse_resp.get("hits", []) if isinstance(browse_resp, dict) else []
                        for raw_obj in hits:
                            documents_found += 1
                            try:
                                doc = normalize_object(
                                    raw_obj,
                                    tenant_id=self.tenant_id,
                                    connector_id=self.connector_id,
                                    index_name=index_name,
                                )
                                await self.ingest_document(
                                    doc, kb_id=kb_id or "", webhook_url=webhook_url
                                )
                                documents_synced += 1
                            except Exception as exc:  # noqa: BLE001
                                logger.error(
                                    "algolia.sync.object_failed",
                                    error=str(exc),
                                    index=index_name,
                                )
                                documents_failed += 1
                        cursor = browse_resp.get("cursor") if isinstance(browse_resp, dict) else None
                        if not cursor:
                            break

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Algolia documents",
            )
        except Exception as exc:  # noqa: BLE001 — surface any unexpected fault
            logger.error(
                "algolia.sync.failed",
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

    async def list_indexes(self) -> Dict[str, Any]:
        """``GET /1/indexes`` — list all indexes on the application."""
        return await with_retry(
            lambda: self.http_client.list_indexes(),
            max_retries=3,
        )

    async def create_index_settings(
        self, index_name: str, settings: Dict[str, Any]
    ) -> Dict[str, Any]:
        """``PUT /1/indexes/{name}/settings`` — create / replace index settings.

        Algolia creates the index on the first settings push; subsequent
        calls replace the settings entirely. Returns ``{taskID, updatedAt}``.
        """
        return await with_retry(
            lambda: self.http_client.create_index_settings(index_name, settings),
            max_retries=3,
        )

    async def get_index_settings(self, index_name: str) -> Dict[str, Any]:
        """``GET /1/indexes/{name}/settings`` — fetch all index settings."""
        return await with_retry(
            lambda: self.http_client.get_index_settings(index_name),
            max_retries=3,
        )

    async def save_object(
        self,
        index_name: str,
        object_data: Dict[str, Any],
        object_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upsert a single object into *index_name*.

        With *object_id* → ``PUT /1/indexes/{name}/{id}`` (overwrite).
        Without            → ``POST /1/indexes/{name}`` (Algolia generates ID).
        Returns ``{taskID, objectID, createdAt|updatedAt}``.
        """
        return await with_retry(
            lambda: self.http_client.save_object(
                index_name=index_name,
                object_data=object_data,
                object_id=object_id,
            ),
            max_retries=3,
        )

    async def save_objects(
        self,
        index_name: str,
        objects: List[Dict[str, Any]],
        action: str = "addObject",
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/batch`` — bulk indexing.

        *action* one of ``addObject`` | ``updateObject`` | ``partialUpdateObject``
        | ``partialUpdateObjectNoCreate`` | ``deleteObject``.
        """
        return await with_retry(
            lambda: self.http_client.save_objects(
                index_name=index_name,
                objects=objects,
                action=action,
            ),
            max_retries=3,
        )

    async def get_object(
        self,
        index_name: str,
        object_id: str,
        attributes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """``GET /1/indexes/{name}/{id}`` — fetch a single object."""
        return await with_retry(
            lambda: self.http_client.get_object(
                index_name=index_name,
                object_id=object_id,
                attributes=attributes,
            ),
            max_retries=3,
        )

    async def delete_object(
        self, index_name: str, object_id: str
    ) -> Dict[str, Any]:
        """``DELETE /1/indexes/{name}/{id}`` — delete a single object."""
        return await with_retry(
            lambda: self.http_client.delete_object(index_name, object_id),
            max_retries=3,
        )

    async def partial_update_object(
        self,
        index_name: str,
        object_id: str,
        attributes: Dict[str, Any],
        create_if_not_exists: bool = True,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/{id}/partial`` — patch object attributes."""
        return await with_retry(
            lambda: self.http_client.partial_update_object(
                index_name=index_name,
                object_id=object_id,
                attributes=attributes,
                create_if_not_exists=create_if_not_exists,
            ),
            max_retries=3,
        )

    async def browse_index(
        self,
        index_name: str,
        *,
        cursor: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/browse`` — cursor-paginated full export."""
        return await with_retry(
            lambda: self.http_client.browse_index(
                index_name=index_name,
                cursor=cursor,
                params=params,
            ),
            max_retries=3,
        )

    async def search_index(
        self,
        index_name: str,
        query: str,
        *,
        filters: Optional[str] = None,
        hits_per_page: int = 20,
        page: int = 0,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/query`` — single-index keyword search."""
        return await with_retry(
            lambda: self.http_client.search_index(
                index_name=index_name,
                query=query,
                filters=filters,
                hits_per_page=hits_per_page,
                page=page,
                extra=extra,
            ),
            max_retries=3,
        )

    async def multi_search(
        self, requests: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """``POST /1/indexes/*/queries`` — federated multi-index search."""
        return await with_retry(
            lambda: self.http_client.multi_search(requests),
            max_retries=3,
        )

    async def list_synonyms(
        self,
        index_name: str,
        *,
        query: str = "",
        type: Optional[str] = None,
        page: int = 0,
        hits_per_page: int = 100,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/synonyms/search``."""
        return await with_retry(
            lambda: self.http_client.list_synonyms(
                index_name=index_name,
                query=query,
                type=type,
                page=page,
                hits_per_page=hits_per_page,
            ),
            max_retries=3,
        )

    async def save_synonym(
        self,
        index_name: str,
        synonym_id: str,
        synonym: Dict[str, Any],
        forward_to_replicas: bool = False,
    ) -> Dict[str, Any]:
        """``PUT /1/indexes/{name}/synonyms/{id}`` — upsert a synonym record."""
        return await with_retry(
            lambda: self.http_client.save_synonym(
                index_name=index_name,
                synonym_id=synonym_id,
                synonym=synonym,
                forward_to_replicas=forward_to_replicas,
            ),
            max_retries=3,
        )

    async def list_rules(
        self,
        index_name: str,
        *,
        query: str = "",
        page: int = 0,
        hits_per_page: int = 100,
    ) -> Dict[str, Any]:
        """``POST /1/indexes/{name}/rules/search``."""
        return await with_retry(
            lambda: self.http_client.list_rules(
                index_name=index_name,
                query=query,
                page=page,
                hits_per_page=hits_per_page,
            ),
            max_retries=3,
        )

    async def save_rule(
        self,
        index_name: str,
        rule_id: str,
        rule: Dict[str, Any],
        forward_to_replicas: bool = False,
    ) -> Dict[str, Any]:
        """``PUT /1/indexes/{name}/rules/{id}`` — upsert a merchandising rule."""
        return await with_retry(
            lambda: self.http_client.save_rule(
                index_name=index_name,
                rule_id=rule_id,
                rule=rule,
                forward_to_replicas=forward_to_replicas,
            ),
            max_retries=3,
        )
