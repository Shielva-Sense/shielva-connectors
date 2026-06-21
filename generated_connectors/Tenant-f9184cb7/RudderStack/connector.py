"""Rudderstack connector — orchestration only.

All HTTP calls       → client/http_client.py
All normalization    → helpers/normalizer.py
All utilities        → helpers/utils.py
All custom errors    → exceptions.py

Auth surfaces (two):
  - Data Plane (HTTP API)   `https://hosted.rudderlabs.com`
      Auth = HTTP Basic with ``write_key`` as username, empty password.
  - Control Plane (Management v2)  `https://api.rudderstack.com/v2`
      Auth = ``Authorization: Bearer <personal_access_token>``.

Required install field: ``write_key`` (data plane is the primary surface).
Optional install field: ``access_token`` (PAT — enables control-plane methods).
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

from client.http_client import RudderstackHTTPClient
from exceptions import (
    RudderstackAuthError,
    RudderstackError,
    RudderstackNotFoundError,
    RudderstackServerError,
)
from helpers.normalizer import normalize_destination, normalize_source
from helpers.utils import iso8601_now, normalize_event_payload, with_retry

logger = structlog.get_logger(__name__)

_DATA_PLANE_BASE = "https://hosted.rudderlabs.com"
_CONTROL_PLANE_BASE = "https://api.rudderstack.com/v2"


class RudderstackConnector(BaseConnector):
    """Shielva connector for the Rudderstack Customer Data Platform.

    - Control plane (sources / destinations / connections / workspaces /
      profiles / identities): Bearer ``access_token``.
    - Data plane (track / identify / page / screen / group / alias / batch):
      HTTP Basic ``write_key``.
    """

    CONNECTOR_TYPE = "rudderstack"
    CONNECTOR_NAME = "Rudderstack"
    AUTH_TYPE = "api_key"

    # Only ``write_key`` is hard-required at install — data plane is the
    # primary surface. ``access_token`` (PAT) is optional; control-plane
    # methods raise ``RudderstackAuthError`` if it is missing at call time.
    REQUIRED_CONFIG_KEYS: List[str] = ["write_key"]

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
        self.write_key: str = self.config.get("write_key", "")
        self.access_token: str = self.config.get("access_token", "")
        self.data_plane_url: str = (
            self.config.get("data_plane_url") or _DATA_PLANE_BASE
        )
        self.control_plane_url: str = (
            self.config.get("control_plane_url") or _CONTROL_PLANE_BASE
        )
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        self.http_client = RudderstackHTTPClient(
            write_key=self.write_key,
            access_token=self.access_token,
            data_plane_url=self.data_plane_url,
            control_plane_url=self.control_plane_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        ``write_key`` is required (primary surface = data-plane event ingest).
        ``access_token`` is optional — without it, control-plane methods will
        raise ``RudderstackAuthError`` at call time.
        """
        if not self.write_key:
            logger.warning(
                "rudderstack.install.missing_write_key",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="write_key is required",
            )

        await self.save_config(
            {
                "write_key": self.write_key,
                "access_token": self.access_token,
                "data_plane_url": self.data_plane_url,
                "control_plane_url": self.control_plane_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("rudderstack.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Rudderstack connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured ``write_key``.
        """
        return TokenInfo(
            access_token=self.write_key or self.access_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Rudderstack connectivity.

        If a PAT is present we probe the control plane (``GET /sources?limit=1``).
        Otherwise we treat the install as healthy if ``write_key`` is set, as
        the data plane has no idempotent GET probe.
        """
        if not self.access_token:
            if self.write_key:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.HEALTHY,
                    auth_status=AuthStatus.CONNECTED,
                    message=(
                        "Rudderstack data plane configured "
                        "(no PAT — control plane unavailable)"
                    ),
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="write_key is required for health check",
            )

        try:
            await with_retry(
                lambda: self.http_client.list_sources(limit=1),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Rudderstack control plane reachable",
            )
        except RudderstackAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Rudderstack auth failed: {exc}",
            )
        except RudderstackServerError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Rudderstack network error: {exc}",
            )
        except RudderstackError as exc:
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
        """Sync control-plane inventory (sources + destinations) to the KB.

        Rudderstack is event-streaming — there is no event corpus to backfill.
        For symmetry with other connectors we ingest the control-plane
        inventory so the KB knows which sources and destinations exist.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        if not self.access_token:
            return SyncResult(
                status=SyncStatus.COMPLETED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message=(
                    "Rudderstack sync skipped — PAT (access_token) not configured; "
                    "data-plane events stream directly."
                ),
            )

        try:
            sources_resp = await with_retry(
                lambda: self.http_client.list_sources(limit=100),
                max_retries=3,
            )
            for raw in sources_resp.get("sources", []) or []:
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
                    logger.error("rudderstack.sync.source_failed", error=str(exc))
                    documents_failed += 1

            dests_resp = await with_retry(
                lambda: self.http_client.list_destinations(limit=100),
                max_retries=3,
            )
            for raw in dests_resp.get("destinations", []) or []:
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
                    logger.error("rudderstack.sync.destination_failed", error=str(exc))
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Rudderstack inventory items",
            )
        except Exception as exc:
            logger.error(
                "rudderstack.sync.failed",
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

    # ── Public API: workspaces ─────────────────────────────────────────────

    async def list_workspaces(self) -> Dict[str, Any]:
        """GET /workspaces — list workspaces accessible to the PAT."""
        return await with_retry(
            lambda: self.http_client.list_workspaces(),
            max_retries=3,
        )

    # ── Public API: sources ────────────────────────────────────────────────

    async def list_sources(
        self,
        limit: int = 50,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /sources — list workspace sources with pagination."""
        return await with_retry(
            lambda: self.http_client.list_sources(limit=limit, after=after),
            max_retries=3,
        )

    async def get_source(self, source_id: str) -> Dict[str, Any]:
        """GET /sources/{id}."""
        return await with_retry(
            lambda: self.http_client.get_source(source_id),
            max_retries=3,
        )

    async def create_source(
        self,
        name: str,
        type: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /sources — create a new source (type e.g. "Javascript", "Python", "HTTP")."""
        return await self.http_client.create_source(
            name=name,
            type_=type,
            config=config,
        )

    # ── Public API: destinations ───────────────────────────────────────────

    async def list_destinations(
        self,
        limit: int = 50,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /destinations."""
        return await with_retry(
            lambda: self.http_client.list_destinations(limit=limit, after=after),
            max_retries=3,
        )

    async def get_destination(self, destination_id: str) -> Dict[str, Any]:
        """GET /destinations/{id}."""
        return await with_retry(
            lambda: self.http_client.get_destination(destination_id),
            max_retries=3,
        )

    # ── Public API: connections ────────────────────────────────────────────

    async def list_connections(self) -> Dict[str, Any]:
        """GET /connections — list source↔destination wirings."""
        return await with_retry(
            lambda: self.http_client.list_connections(),
            max_retries=3,
        )

    # ── Public API: profiles / identities ──────────────────────────────────

    async def list_profiles(
        self,
        limit: int = 50,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /profiles — list unified user profiles (Profiles API)."""
        return await with_retry(
            lambda: self.http_client.list_profiles(limit=limit, after=after),
            max_retries=3,
        )

    async def get_profile(self, profile_id: str) -> Dict[str, Any]:
        """GET /profiles/{id}."""
        return await with_retry(
            lambda: self.http_client.get_profile(profile_id),
            max_retries=3,
        )

    async def list_identities(
        self,
        limit: int = 50,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /identities — list identity records across sources."""
        return await with_retry(
            lambda: self.http_client.list_identities(limit=limit, after=after),
            max_retries=3,
        )

    # ── Public API: data-plane events ──────────────────────────────────────

    async def track_event(
        self,
        user_id: str,
        event: str,
        properties: Optional[Dict[str, Any]] = None,
        write_key: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST {data_plane}/v1/track — record a user action."""
        payload = normalize_event_payload(
            user_id=user_id,
            extra={"event": event, "properties": properties or {}},
            timestamp=timestamp,
        )
        return await self.http_client.post_event(
            "/v1/track",
            payload,
            write_key=write_key,
        )

    async def identify_user(
        self,
        user_id: str,
        traits: Optional[Dict[str, Any]] = None,
        write_key: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST {data_plane}/v1/identify — set user identity + traits."""
        payload = normalize_event_payload(
            user_id=user_id,
            extra={"traits": traits or {}},
            timestamp=timestamp,
        )
        return await self.http_client.post_event(
            "/v1/identify",
            payload,
            write_key=write_key,
        )

    async def page_event(
        self,
        user_id: str,
        name: Optional[str] = None,
        properties: Optional[Dict[str, Any]] = None,
        write_key: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST {data_plane}/v1/page — record a web page-view event."""
        extra: Dict[str, Any] = {"properties": properties or {}}
        if name:
            extra["name"] = name
        payload = normalize_event_payload(
            user_id=user_id,
            extra=extra,
            timestamp=timestamp,
        )
        return await self.http_client.post_event(
            "/v1/page",
            payload,
            write_key=write_key,
        )

    async def screen_event(
        self,
        user_id: str,
        name: Optional[str] = None,
        properties: Optional[Dict[str, Any]] = None,
        write_key: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST {data_plane}/v1/screen — record a mobile screen-view event."""
        extra: Dict[str, Any] = {"properties": properties or {}}
        if name:
            extra["name"] = name
        payload = normalize_event_payload(
            user_id=user_id,
            extra=extra,
            timestamp=timestamp,
        )
        return await self.http_client.post_event(
            "/v1/screen",
            payload,
            write_key=write_key,
        )

    async def group_event(
        self,
        user_id: str,
        group_id: str,
        traits: Optional[Dict[str, Any]] = None,
        write_key: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST {data_plane}/v1/group — associate user with a group/account."""
        payload = normalize_event_payload(
            user_id=user_id,
            extra={"groupId": group_id, "traits": traits or {}},
            timestamp=timestamp,
        )
        return await self.http_client.post_event(
            "/v1/group",
            payload,
            write_key=write_key,
        )

    async def alias_user(
        self,
        user_id: str,
        previous_id: str,
        write_key: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST {data_plane}/v1/alias — link a new userId to a previousId."""
        payload = normalize_event_payload(
            user_id=user_id,
            extra={"previousId": previous_id},
            timestamp=timestamp,
        )
        return await self.http_client.post_event(
            "/v1/alias",
            payload,
            write_key=write_key,
        )

    async def batch_events(
        self,
        events: List[Dict[str, Any]],
        write_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST {data_plane}/v1/batch — submit multiple events in one request.

        Each entry in ``events`` must already be a fully-formed event with a
        ``type`` field ("track" | "identify" | "page" | "screen" | "group" |
        "alias") plus the relevant fields. Rudderstack accepts the standard
        Segment-compatible envelope.
        """
        payload: Dict[str, Any] = {"batch": events, "sentAt": iso8601_now()}
        return await self.http_client.post_event(
            "/v1/batch",
            payload,
            write_key=write_key,
        )
