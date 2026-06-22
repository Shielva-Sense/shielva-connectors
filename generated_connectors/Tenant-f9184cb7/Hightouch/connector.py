"""Hightouch connector — orchestration only.

All HTTP calls       → client/http_client.py
All normalization    → helpers/normalizer.py
All utilities        → helpers/utils.py
All custom errors    → exceptions.py

Auth surface (one):
  - REST API (`https://api.hightouch.com/api/v1`)
      Auth = ``Authorization: Bearer <api_token>``.

Required install field: ``api_token``.
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

from client.http_client import HightouchHTTPClient
from exceptions import (
    HightouchAuthError,
    HightouchError,
    HightouchNotFoundError,
    HightouchServerError,
)
from helpers.normalizer import (
    normalize_destination,
    normalize_model,
    normalize_source,
    normalize_sync,
)
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_HIGHTOUCH_BASE = "https://api.hightouch.com/api/v1"


class HightouchConnector(BaseConnector):
    """Shielva connector for the Hightouch Reverse-ETL / Composable CDP."""

    CONNECTOR_TYPE = "hightouch"
    CONNECTOR_NAME = "Hightouch"
    AUTH_TYPE = "api_key"

    # Only ``api_token`` is hard-required at install. Base URL and rate cap
    # have defaults.
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
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_token: str = self.config.get("api_token", "") or self.config.get(
            "api_key", ""
        )
        self.base_url: str = self.config.get("base_url") or _HIGHTOUCH_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = HightouchHTTPClient(
            api_token=self.api_token,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        ``api_token`` is required (single auth surface for Hightouch).
        """
        if not self.api_token:
            logger.warning(
                "hightouch.install.missing_api_token",
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
                "api_token": self.api_token,
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("hightouch.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Hightouch connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured ``api_token``.
        """
        return TokenInfo(
            access_token=self.api_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Hightouch connectivity by probing ``GET /workspaces``."""
        if not self.api_token:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required for health check",
            )

        try:
            await with_retry(
                lambda: self.http_client.list_workspaces(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Hightouch API reachable",
            )
        except HightouchAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Hightouch auth failed: {exc}",
            )
        except HightouchServerError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Hightouch network error: {exc}",
            )
        except HightouchError as exc:
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
        """Sync Hightouch inventory (sources + models + destinations + syncs) to the KB.

        Hightouch is reverse-ETL — there is no row corpus to backfill. For
        symmetry with other connectors we ingest the workspace inventory so
        the KB knows which pipelines exist.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            sources_resp = await with_retry(
                lambda: self.http_client.list_sources(page=1, per_page=100),
                max_retries=3,
            )
            for raw in self._unwrap_list(sources_resp, "sources"):
                documents_found += 1
                try:
                    doc = normalize_source(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc,
                        kb_id=kb_id or "",
                        webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("hightouch.sync.source_failed", error=str(exc))
                    documents_failed += 1

            models_resp = await with_retry(
                lambda: self.http_client.list_models(page=1, per_page=100),
                max_retries=3,
            )
            for raw in self._unwrap_list(models_resp, "models"):
                documents_found += 1
                try:
                    doc = normalize_model(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc,
                        kb_id=kb_id or "",
                        webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("hightouch.sync.model_failed", error=str(exc))
                    documents_failed += 1

            dests_resp = await with_retry(
                lambda: self.http_client.list_destinations(page=1, per_page=100),
                max_retries=3,
            )
            for raw in self._unwrap_list(dests_resp, "destinations"):
                documents_found += 1
                try:
                    doc = normalize_destination(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc,
                        kb_id=kb_id or "",
                        webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("hightouch.sync.destination_failed", error=str(exc))
                    documents_failed += 1

            syncs_resp = await with_retry(
                lambda: self.http_client.list_syncs(page=1, per_page=100),
                max_retries=3,
            )
            for raw in self._unwrap_list(syncs_resp, "syncs"):
                documents_found += 1
                try:
                    doc = normalize_sync(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc,
                        kb_id=kb_id or "",
                        webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("hightouch.sync.sync_failed", error=str(exc))
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=(
                    f"Synced {documents_synced}/{documents_found} "
                    "Hightouch inventory items"
                ),
            )
        except Exception as exc:
            logger.error(
                "hightouch.sync.failed",
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

    @staticmethod
    def _unwrap_list(resp: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
        """Hightouch list endpoints variously wrap rows in `data`, `<key>`, or `results`."""
        if not isinstance(resp, dict):
            return []
        for candidate in (key, "data", "results"):
            value = resp.get(candidate)
            if isinstance(value, list):
                return value
        return []

    # ── Public API: workspaces ─────────────────────────────────────────────

    async def list_workspaces(self) -> Dict[str, Any]:
        """GET /workspaces — list workspaces accessible to the API token."""
        return await with_retry(
            lambda: self.http_client.list_workspaces(),
            max_retries=3,
        )

    # ── Public API: sources ────────────────────────────────────────────────

    async def list_sources(
        self,
        page: int = 1,
        per_page: int = 50,
        slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /sources — list workspace sources."""
        return await with_retry(
            lambda: self.http_client.list_sources(
                page=page, per_page=per_page, slug=slug
            ),
            max_retries=3,
        )

    async def get_source(self, source_id: Any) -> Dict[str, Any]:
        """GET /sources/{id}."""
        return await with_retry(
            lambda: self.http_client.get_source(source_id),
            max_retries=3,
        )

    # ── Public API: models ─────────────────────────────────────────────────

    async def list_models(
        self,
        page: int = 1,
        per_page: int = 50,
        slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /models — list workspace models."""
        return await with_retry(
            lambda: self.http_client.list_models(
                page=page, per_page=per_page, slug=slug
            ),
            max_retries=3,
        )

    async def get_model(self, model_id: Any) -> Dict[str, Any]:
        """GET /models/{id}."""
        return await with_retry(
            lambda: self.http_client.get_model(model_id),
            max_retries=3,
        )

    # ── Public API: destinations ───────────────────────────────────────────

    async def list_destinations(
        self,
        page: int = 1,
        per_page: int = 50,
        slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /destinations."""
        return await with_retry(
            lambda: self.http_client.list_destinations(
                page=page, per_page=per_page, slug=slug
            ),
            max_retries=3,
        )

    async def get_destination(self, destination_id: Any) -> Dict[str, Any]:
        """GET /destinations/{id}."""
        return await with_retry(
            lambda: self.http_client.get_destination(destination_id),
            max_retries=3,
        )

    # ── Public API: syncs ──────────────────────────────────────────────────

    async def list_syncs(
        self,
        page: int = 1,
        per_page: int = 50,
        slug: Optional[str] = None,
        model_id: Optional[Any] = None,
        destination_id: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """GET /syncs — list workspace syncs (optionally filtered)."""
        return await with_retry(
            lambda: self.http_client.list_syncs(
                page=page,
                per_page=per_page,
                slug=slug,
                model_id=model_id,
                destination_id=destination_id,
            ),
            max_retries=3,
        )

    async def get_sync(self, sync_id: Any) -> Dict[str, Any]:
        """GET /syncs/{id}."""
        return await with_retry(
            lambda: self.http_client.get_sync(sync_id),
            max_retries=3,
        )

    async def create_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /syncs — create a new sync from a raw payload."""
        return await self.http_client.create_sync(payload)

    async def run_sync(
        self,
        sync_id: Any,
        full_resync: bool = False,
    ) -> Dict[str, Any]:
        """POST /syncs/{id}/trigger — kick off a sync run (`fullResync=true` to re-sync all rows)."""
        return await self.http_client.run_sync(sync_id, full_resync=full_resync)

    # ── Public API: sync runs ──────────────────────────────────────────────

    async def list_sync_runs(
        self,
        sync_id: Any,
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """GET /syncs/{id}/runs."""
        return await with_retry(
            lambda: self.http_client.list_sync_runs(
                sync_id, page=page, per_page=per_page
            ),
            max_retries=3,
        )

    async def get_sync_run(
        self,
        sync_id: Any,
        run_id: Any,
    ) -> Dict[str, Any]:
        """GET /syncs/{sid}/runs/{rid}."""
        return await with_retry(
            lambda: self.http_client.get_sync_run(sync_id, run_id),
            max_retries=3,
        )

    # ── Public API: sequences ──────────────────────────────────────────────

    async def list_sequences(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """GET /sequences — list orchestrated multi-step sync sequences."""
        return await with_retry(
            lambda: self.http_client.list_sequences(page=page, per_page=per_page),
            max_retries=3,
        )

    # ── Public API: events (Customer Studio) ──────────────────────────────

    async def send_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """POST /events — forward a single Customer Studio event."""
        return await self.http_client.send_event(event)
