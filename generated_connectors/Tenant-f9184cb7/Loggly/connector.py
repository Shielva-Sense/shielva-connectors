"""Loggly connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: HTTP Basic for management API; URL-token for bulk send. Required headers:
    Authorization: Basic base64(username:password)    (management only)
    Content-Type:  application/json
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

from client.http_client import LogglyHTTPClient
from exceptions import (
    LogglyAuthError,
    LogglyError,
    LogglyNotFoundError,
    LogglyServerError,
)
from helpers.normalizer import normalize_event
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_DEFAULT_INGEST_BASE = "https://logs-01.loggly.com"


class LogglyConnector(BaseConnector):
    """Shielva connector for SolarWinds Loggly (log management + search + bulk ingest)."""

    CONNECTOR_TYPE = "loggly"
    CONNECTOR_NAME = "Loggly"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "subdomain",
        "username",
        "password",
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
        self.subdomain: str = self.config.get("subdomain", "")
        self.username: str = self.config.get("username", "")
        self.password: str = self.config.get("password", "")
        self.customer_token: str = self.config.get("customer_token", "")
        self.base_url: str = self.config.get("base_url", "")
        self.ingest_base_url: str = (
            self.config.get("ingest_base_url", "") or _DEFAULT_INGEST_BASE
        )
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = LogglyHTTPClient(
            subdomain=self.subdomain,
            username=self.username,
            password=self.password,
            customer_token=self.customer_token,
            base_url=self.base_url,
            ingest_base_url=self.ingest_base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Loggly install only requires `subdomain` + `username` + `password`.
        `customer_token` is optional (needed only for `send_events_bulk`).
        """
        subdomain = self.config.get("subdomain")
        username = self.config.get("username")
        password = self.config.get("password")

        if not subdomain or not username or not password:
            logger.warning(
                "loggly.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="subdomain, username, and password are required",
            )

        await self.save_config(
            {
                "subdomain": subdomain,
                "username": username,
                "password": password,
                "customer_token": self.config.get("customer_token", ""),
                "base_url": self.config.get("base_url", ""),
                "ingest_base_url": self.config.get(
                    "ingest_base_url", _DEFAULT_INGEST_BASE
                ),
            }
        )
        logger.info("loggly.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Loggly connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key/Basic-auth connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI.
        """
        return TokenInfo(
            access_token=self.username,  # opaque identifier; actual auth = Basic header
            refresh_token=None,
            expires_at=None,
            token_type="basic",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Loggly API connectivity by running a 1-row search."""
        try:
            await with_retry(
                lambda: self.http_client.search_logs(q="*", size=1),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Loggly API reachable",
            )
        except LogglyAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Loggly auth failed: {exc}",
            )
        except LogglyServerError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Loggly network/server error: {exc}",
            )
        except LogglyError as exc:
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
        """Sync Loggly events into the Shielva KB via the events/iterate cursor."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Time-window: explicit `since` overrides the default 24h window
            # only when caller passes one.
            from_window = (
                since.isoformat() if isinstance(since, datetime) else "-24h"
            )
            next_cursor: Optional[str] = None
            for _ in range(50):  # hard cap on pages per sync
                resp = await with_retry(
                    lambda c=next_cursor: self.http_client.iterate_events(
                        q="*",
                        from_=from_window,
                        until="now",
                        size=100,
                        next_=c,
                    ),
                    max_retries=3,
                )
                events = resp.get("events") or []
                for raw in events:
                    documents_found += 1
                    try:
                        doc = normalize_event(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("loggly.sync.event_failed", error=str(exc))
                        documents_failed += 1
                next_cursor = resp.get("next")
                if not next_cursor or not events:
                    break

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Loggly events",
            )
        except Exception as exc:
            logger.error(
                "loggly.sync.failed", error=str(exc), connector_id=self.connector_id
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def search_logs(
        self,
        q: str = "*",
        from_: str = "-24h",
        until: str = "now",
        size: int = 100,
        order: str = "desc",
    ) -> Dict[str, Any]:
        """GET /apiv2/search — search logs by time window + query."""
        return await with_retry(
            lambda: self.http_client.search_logs(
                q=q, from_=from_, until=until, size=size, order=order
            ),
            max_retries=3,
        )

    async def get_search_field_stats(
        self,
        field: str,
        q: str = "*",
        from_: str = "-24h",
        until: str = "now",
        facet_size: int = 100,
    ) -> Dict[str, Any]:
        """GET /apiv2/fields/{field} — field-level aggregation."""
        return await with_retry(
            lambda: self.http_client.get_search_field_stats(
                field=field, q=q, from_=from_, until=until, facet_size=facet_size,
            ),
            max_retries=3,
        )

    async def list_saved_searches(self) -> Dict[str, Any]:
        """GET /apiv2/savedsearches."""
        return await with_retry(
            lambda: self.http_client.list_saved_searches(),
            max_retries=3,
        )

    async def create_saved_search(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /apiv2/savedsearches."""
        return await self.http_client.create_saved_search(payload)

    async def list_alerts(self) -> Dict[str, Any]:
        """GET /apiv2/alerts."""
        return await with_retry(
            lambda: self.http_client.list_alerts(),
            max_retries=3,
        )

    async def create_alert(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /apiv2/alerts."""
        return await self.http_client.create_alert(payload)

    async def list_dashboards(self) -> Dict[str, Any]:
        """GET /apiv2/dashboards."""
        return await with_retry(
            lambda: self.http_client.list_dashboards(),
            max_retries=3,
        )

    async def get_dashboard(self, dashboard_id: str) -> Dict[str, Any]:
        """GET /apiv2/dashboards/{id}."""
        return await with_retry(
            lambda: self.http_client.get_dashboard(dashboard_id),
            max_retries=3,
        )

    async def list_source_groups(self) -> Dict[str, Any]:
        """GET /apiv2/sourcegroups."""
        return await with_retry(
            lambda: self.http_client.list_source_groups(),
            max_retries=3,
        )

    async def list_users(self) -> Dict[str, Any]:
        """GET /apiv2/users."""
        return await with_retry(
            lambda: self.http_client.list_users(),
            max_retries=3,
        )

    async def send_events_bulk(
        self,
        events: List[Dict[str, Any]],
        tag: str = "bulk",
    ) -> Dict[str, Any]:
        """POST https://logs-01.loggly.com/bulk/{token}/tag/{tag}/ — NDJSON body.

        Requires `customer_token` install_field.
        """
        return await self.http_client.send_events_bulk(events=events, tag=tag)
