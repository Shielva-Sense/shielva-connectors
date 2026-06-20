from __future__ import annotations

from typing import Any, Dict
from urllib.parse import urlencode

from client import WebexHTTPClient
from exceptions import WebexAuthError, WebexError, WebexNetworkError
from helpers import (
    CircuitBreaker,
    normalize_meeting,
    normalize_room,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: Dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

WEBEX_OAUTH_AUTH_URL = "https://webexapis.com/v1/authorize"
WEBEX_OAUTH_TOKEN_URL = "https://webexapis.com/v1/access_token"
WEBEX_OAUTH_SCOPES = (
    "spark:all spark:messages_read spark:rooms_read "
    "spark:memberships_read meeting:schedules_read"
)
SYNC_PAGE_SIZE = 100
CIRCUIT_BREAKER_THRESHOLD = 5


class WebexConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Cisco Webex.

    Provides OAuth2 authentication, health checks, full sync of rooms and
    meetings via the Webex REST API.
    """

    CONNECTOR_TYPE: str = "webex"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        try:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        except TypeError:
            self.config = _config
            self.connector_id = connector_id
            self.tenant_id = tenant_id
        # Webex-specific attrs
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")
        self._token_expires_at: str = _config.get("token_expires_at", "")
        self.http_client: WebexHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> WebexHTTPClient:
        return WebexHTTPClient(access_token=self._access_token)

    def _has_credentials(self) -> bool:
        """True when we have enough to authenticate."""
        return bool(self._access_token or (self._client_id and self._client_secret))

    def _ensure_client(self) -> WebexHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate client_id and client_secret are present."""
        if not self._client_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id is required",
            )
        if not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_secret is required",
            )
        if self._access_token:
            client = self._make_client()
            try:
                await with_retry(client.get_me)
                await client.aclose()
                self.http_client = self._make_client()
                return InstallResult(
                    health=ConnectorHealth.HEALTHY,
                    auth_status=AuthStatus.CONNECTED,
                    connector_id=self.connector_id,
                    message="Connected to Webex API",
                )
            except WebexAuthError as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Webex authentication failed: {exc}",
                )
            except Exception as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.FAILED,
                    message=str(exc),
                )
        # No access token yet — OAuth flow must be completed via authorize()
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Webex OAuth credentials accepted. Complete authorization via the OAuth flow.",
        )

    def authorize(self) -> str:
        """Return the Webex OAuth2 authorization URL."""
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
            "scope": WEBEX_OAUTH_SCOPES,
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        return f"{WEBEX_OAUTH_AUTH_URL}?{urlencode(params)}"

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /people/me and return current health."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret (or access_token) are required",
            )
        if not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="OAuth flow not completed — access_token is missing",
            )
        client = self._make_client()
        try:
            me = await with_retry(client.get_me)
            await client.aclose()
            self._circuit_breaker.on_success()
            display_name = me.get("displayName", "")
            email = me.get("emails", [""])[0] if me.get("emails") else ""
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected as {display_name} ({email})" if display_name else "Webex API is reachable",
            )
        except WebexAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except WebexNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = (
                ConnectorHealth.DEGRADED
                if not self._circuit_breaker.is_open
                else ConnectorHealth.OFFLINE
            )
            return HealthCheckResult(
                health=health,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync Webex rooms and meetings into the knowledge base."""
        kb_id: str = kwargs.get("kb_id", "")

        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync rooms
        try:
            rooms = await self._fetch_all_rooms()
        except WebexError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        found += len(rooms)
        for record in rooms:
            try:
                doc = normalize_room(record, self.connector_id, self.tenant_id)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # Sync meetings
        from_date: str | None = kwargs.get("from_date")
        try:
            meetings = await self._fetch_all_meetings(from_date=from_date)
        except WebexError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        found += len(meetings)
        for record in meetings:
            try:
                doc = normalize_meeting(record, self.connector_id, self.tenant_id)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_rooms(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                self.http_client.get_rooms,
                max=SYNC_PAGE_SIZE,
                cursor=cursor,
            )
            records.extend(page.get("items", []))
            cursor = page.get("nextCursor") or page.get("cursor")
            if not cursor:
                break
        return records

    async def _fetch_all_meetings(
        self, from_date: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                self.http_client.get_meetings,
                cursor=cursor,
                from_date=from_date,
            )
            records.extend(page.get("items", []))
            cursor = page.get("nextCursor") or page.get("cursor")
            if not cursor:
                break
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Public API methods ────────────────────────────────────────────────────

    async def list_rooms(
        self,
        max: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_rooms, max=max, cursor=cursor)

    async def list_meetings(
        self,
        from_date: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_meetings, cursor=cursor, from_date=from_date)

    async def list_people(
        self,
        cursor: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_people, cursor=cursor, email=email)

    async def get_room(self, room_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_room, room_id)

    async def list_messages(
        self,
        room_id: str,
        max: int = 100,
        before_message: str | None = None,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(
            client.get_messages,
            room_id=room_id,
            max=max,
            before_message=before_message,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> WebexConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
