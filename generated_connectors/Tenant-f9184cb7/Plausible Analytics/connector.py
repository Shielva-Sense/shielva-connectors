"""Plausible Analytics connector — orchestration only.

All HTTP calls       → client/http_client.py
All normalization    → helpers/normalizer.py
All shared utilities → helpers/utils.py

Auth model:
  • Stats + Sites API → Authorization: Bearer <api_key>
  • Events API        → no auth; identity derived from User-Agent header
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
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import PlausibleHTTPClient
from exceptions import (
    PlausibleAPIError,
    PlausibleAuthError,
    PlausibleError,
    PlausibleNetworkError,
    PlausibleNotFound,
)
from helpers.normalizer import normalize_breakdown_row, normalize_site_snapshot
from helpers.utils import default_metrics

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://plausible.io/api/v1"
_DEFAULT_USER_AGENT = "Shielva/1.0"


class PlausibleConnector(BaseConnector):
    """Shielva connector for the Plausible Analytics REST API."""

    CONNECTOR_TYPE = "plausible"
    CONNECTOR_NAME = "Plausible Analytics"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

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
        self.api_key: str = self.config.get("api_key", "")
        # Accept either the host (https://plausible.io) or full base
        # (https://plausible.io/api/v1) — normalise to the full form.
        raw_base = self.config.get("base_url") or _DEFAULT_BASE_URL
        if raw_base.rstrip("/").endswith("/api/v1"):
            self.base_url: str = raw_base.rstrip("/")
        else:
            self.base_url = raw_base.rstrip("/") + "/api/v1"
        self.default_site_id: str = self.config.get("default_site_id", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 600)

        self.http_client = PlausibleHTTPClient(
            base_url=self.base_url,
            api_key=self.api_key,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate the install-time config and persist it.

        Plausible API-key install only requires `api_key`; site/base_url are
        optional defaults used by `health_check` + `sync`.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning(
                "plausible.install.missing_credentials",
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
                "base_url": self.base_url,
                "default_site_id": self.default_site_id,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("plausible.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Plausible connector installed",
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
        """Verify the API key by hitting realtime visitors on default_site_id.

        Falls back to a Sites probe when `default_site_id` is not configured,
        so health_check is never a hard failure for misconfigured tenants.
        """
        site_id = self.default_site_id
        try:
            if site_id:
                await self.http_client.get_realtime_visitors(site_id)
            else:
                await self.http_client.list_sites()
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Plausible API reachable",
            )
        except PlausibleAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except PlausibleNotFound as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Site not found: {exc}",
            )
        except PlausibleNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Plausible network error: {exc}",
            )
        except PlausibleError as exc:
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
        """Snapshot the default site's headline KPIs into a NormalizedDocument.

        Plausible is an analytics product — there are no documents to ingest
        in the traditional sense. We materialise a single per-site snapshot
        document (30d aggregate + realtime) so the platform's standard sync
        surfaces (search, KB) stay usable.
        """
        site_id = self.default_site_id
        if not site_id:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message="default_site_id is required for sync",
            )
        try:
            agg = await self.http_client.get_aggregate(
                site_id=site_id,
                period="30d",
                metrics=default_metrics("aggregate"),
            )
            rt = await self.http_client.get_realtime_visitors(site_id)
            visitors = (
                (agg.get("results", {}) or {}).get("visitors", {}) or {}
            ).get("value", 0) or 0

            doc = normalize_site_snapshot(
                site_id=site_id,
                aggregate=agg,
                realtime=rt,
                connector_id=self.connector_id,
                tenant_id=self.tenant_id,
            )
            try:
                await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                synced = 1
                failed = 0
            except Exception as exc:  # pragma: no cover — defensive ingest path
                logger.error("plausible.sync.ingest_failed", error=str(exc))
                synced = 0
                failed = 1

            return SyncResult(
                status=SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL,
                documents_found=1,
                documents_synced=synced,
                documents_failed=failed,
                message=(
                    f"Aggregated 30d snapshot for {site_id}: "
                    f"visitors={int(visitors)}, realtime={rt.get('visitors', 0)}"
                ),
            )
        except PlausibleError as exc:
            logger.error("plausible.sync.failed", error=str(exc))
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message=str(exc),
            )

    # ── Stats API ──────────────────────────────────────────────────────────

    async def aggregate(
        self,
        site_id: str,
        period: str = "30d",
        date: Optional[str] = None,
        metrics: Optional[List[str]] = None,
        filters: Optional[str] = None,
        compare: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return aggregate stats for *site_id* over *period*.

        Defaults to ``visitors,pageviews,bounce_rate,visit_duration`` so
        callers get a useful baseline KPI set without extra round-trips.
        """
        return await self.http_client.get_aggregate(
            site_id=site_id,
            period=period,
            date=date,
            metrics=metrics or default_metrics("aggregate"),
            filters=filters,
            compare=compare,
        )

    # Convenience aliases — match the method-name spec
    async def get_aggregate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Alias for :meth:`aggregate` matching the spec method name."""
        return await self.aggregate(*args, **kwargs)

    async def timeseries(
        self,
        site_id: str,
        period: str = "30d",
        date: Optional[str] = None,
        metrics: Optional[List[str]] = None,
        filters: Optional[str] = None,
        interval: str = "date",
    ) -> Dict[str, Any]:
        """Return a time-bucketed series for *metrics* over *period*."""
        return await self.http_client.get_timeseries(
            site_id=site_id,
            period=period,
            date=date,
            metrics=metrics or default_metrics("timeseries"),
            filters=filters,
            interval=interval,
        )

    async def get_timeseries(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Alias for :meth:`timeseries` matching the spec method name."""
        return await self.timeseries(*args, **kwargs)

    async def breakdown(
        self,
        site_id: str,
        period: str = "30d",
        date: Optional[str] = None,
        property: str = "event:page",
        metrics: Optional[List[str]] = None,
        filters: Optional[str] = None,
        page: int = 1,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Return a paginated breakdown of *property* values with *metrics*.

        The raw response is returned unchanged plus a ``normalized`` sibling
        key that splits each row into ``{dimension, metrics}`` so downstream
        components do not need to know the property prefix.
        """
        raw = await self.http_client.get_breakdown(
            site_id=site_id,
            period=period,
            date=date,
            property=property,
            metrics=metrics or default_metrics("breakdown"),
            filters=filters,
            page=page,
            limit=limit,
        )
        rows = raw.get("results") if isinstance(raw, dict) else None
        if isinstance(rows, list):
            raw["normalized"] = [normalize_breakdown_row(r, property) for r in rows]
        return raw

    async def get_breakdown(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Alias for :meth:`breakdown` matching the spec method name."""
        return await self.breakdown(*args, **kwargs)

    async def realtime_visitors(self, site_id: str) -> Dict[str, Any]:
        """Return the live visitor count for *site_id*."""
        return await self.http_client.get_realtime_visitors(site_id)

    async def get_realtime_visitors(self, site_id: str) -> Dict[str, Any]:
        """Alias for :meth:`realtime_visitors`."""
        return await self.realtime_visitors(site_id)

    # ── Events API (no Bearer auth) ────────────────────────────────────────

    async def record_pageview(
        self,
        domain: str,
        url: str,
        user_agent: str = _DEFAULT_USER_AGENT,
        screen_width: Optional[int] = None,
        referrer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a pageview against *domain* — wraps Plausible's events endpoint.

        Plausible identifies the source from the User-Agent header, NOT the
        API key, so callers should forward the visitor's real user-agent when
        available.
        """
        return await self.http_client.post_event(
            domain=domain,
            name="pageview",
            url=url,
            user_agent=user_agent,
            referrer=referrer,
            screen_width=screen_width,
        )

    async def record_custom_event(
        self,
        domain: str,
        name: str,
        url: str,
        user_agent: str = _DEFAULT_USER_AGENT,
        props: Optional[Dict[str, Any]] = None,
        referrer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a named custom event with optional ``props``."""
        return await self.http_client.post_event(
            domain=domain,
            name=name,
            url=url,
            user_agent=user_agent,
            referrer=referrer,
            props=props,
        )

    async def send_event(
        self,
        domain: str,
        name: str,
        url: str,
        user_agent: str = _DEFAULT_USER_AGENT,
        props: Optional[Dict[str, Any]] = None,
        referrer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Spec-aligned alias for :meth:`record_custom_event`."""
        return await self.record_custom_event(
            domain=domain,
            name=name,
            url=url,
            user_agent=user_agent,
            props=props,
            referrer=referrer,
        )

    # ── Sites Provisioning API ─────────────────────────────────────────────

    async def list_sites(self) -> Dict[str, Any]:
        """List sites visible to the API key."""
        return await self.http_client.list_sites()

    async def get_site(self, site_id: str) -> Dict[str, Any]:
        """Fetch a single site by ID (domain)."""
        return await self.http_client.get_site(site_id)

    async def create_site(self, domain: str, timezone: str = "UTC") -> Dict[str, Any]:
        """Provision a new tracked site."""
        return await self.http_client.create_site(domain=domain, timezone=timezone)

    async def update_site(
        self,
        site_id: str,
        timezone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a site's writable fields (currently only ``timezone``)."""
        return await self.http_client.update_site(site_id=site_id, timezone=timezone)

    async def delete_site(self, site_id: str) -> Dict[str, Any]:
        """Delete a tracked site by ID."""
        return await self.http_client.delete_site(site_id)

    # ── Goals ──────────────────────────────────────────────────────────────

    async def list_goals(self, site_id: str) -> Dict[str, Any]:
        """List conversion goals configured on *site_id*."""
        return await self.http_client.list_goals(site_id)

    async def create_goal(
        self,
        site_id: str,
        goal_type: str,
        event_name: Optional[str] = None,
        page_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create an ``event`` goal (``event_name``) or a ``page`` goal (``page_path``)."""
        if goal_type == "event" and not event_name:
            raise PlausibleAPIError(
                "event_name is required when goal_type='event'",
                status_code=400,
            )
        if goal_type == "page" and not page_path:
            raise PlausibleAPIError(
                "page_path is required when goal_type='page'",
                status_code=400,
            )
        return await self.http_client.create_goal(
            site_id=site_id,
            goal_type=goal_type,
            event_name=event_name,
            page_path=page_path,
        )
