from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import ZoomHTTPClient
from exceptions import ZoomAuthError, ZoomError, ZoomNetworkError
from helpers import (
    CircuitBreaker,
    normalize_meeting,
    normalize_recording,
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
        def __init__(self, tenant_id: str = "", connector_id: str = "", config: dict | None = None) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

CONNECTOR_TYPE = "zoom"
AUTH_TYPE = "api_key"

SYNC_PAGE_SIZE = 300
CIRCUIT_BREAKER_THRESHOLD = 5


class ZoomConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Zoom using Server-to-Server OAuth.

    No user redirect required — credentials are account_id + client_id + client_secret.
    The connector exchanges those for a Bearer token on first use and refreshes automatically.
    """

    CONNECTOR_TYPE: str = "zoom"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._account_id: str = _config.get("account_id", "")
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        # Legacy OAuth2 fields kept for backward compat with existing tests
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")
        self.http_client: ZoomHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> ZoomHTTPClient:
        return ZoomHTTPClient(config=self.config)

    def _has_credentials(self) -> bool:
        """True when enough credentials are present to authenticate."""
        # Server-to-Server: need account_id + client_id + client_secret
        if self._account_id and self._client_id and self._client_secret:
            return True
        # Legacy: access_token alone is sufficient
        if self._access_token:
            return True
        # Legacy: client_id + client_secret without account_id (OAuth2 flow)
        if self._client_id and self._client_secret:
            return True
        return False

    def _basic_auth_header(self) -> str:
        """Build the Basic auth header value for OAuth token exchange."""
        import base64
        raw = f"{self._client_id}:{self._client_secret}"
        return "Basic " + base64.b64encode(raw.encode()).decode()

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials and probe the Zoom API.

        For Server-to-Server OAuth (account_id + client_id + client_secret):
          - Exchange credentials for a token, then probe GET /accounts/me.

        For legacy OAuth2 (client_id + client_secret + existing access_token):
          - Probe GET /accounts/me using the stored token.

        For client_id + client_secret without account_id or access_token:
          - Credentials accepted; token exchange deferred.
        """
        if not (self._client_id and self._client_secret):
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )

        # Server-to-Server path: try to exchange credentials for a token
        if self._account_id:
            client = self._make_client()
            try:
                await with_retry(client.get_token)
                await with_retry(client.get_account_info)
                await client.aclose()
                self.http_client = self._make_client()
                return InstallResult(
                    health=ConnectorHealth.HEALTHY,
                    auth_status=AuthStatus.CONNECTED,
                    connector_id=self.connector_id,
                    message="Connected to Zoom API",
                )
            except ZoomAuthError as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Zoom authentication failed: {exc}",
                )
            except Exception as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.FAILED,
                    message=str(exc),
                )

        # Legacy: existing access_token without account_id
        if self._access_token:
            client = self._make_client()
            try:
                await with_retry(client.get_account_info)
                await client.aclose()
                self.http_client = self._make_client()
                return InstallResult(
                    health=ConnectorHealth.HEALTHY,
                    auth_status=AuthStatus.CONNECTED,
                    connector_id=self.connector_id,
                    message="Connected to Zoom API",
                )
            except ZoomAuthError as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Zoom authentication failed: {exc}",
                )
            except Exception as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.FAILED,
                    message=str(exc),
                )

        # client_id + client_secret only — credentials accepted; OAuth flow pending
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Zoom OAuth credentials accepted. Complete authorization via the OAuth flow.",
        )

    def authorize(self) -> str:
        """Return the Zoom OAuth2 authorization URL (legacy user-redirect flow)."""
        from urllib.parse import urlencode
        ZOOM_OAUTH_AUTH_URL = "https://zoom.us/oauth/authorize"
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        return f"{ZOOM_OAUTH_AUTH_URL}?{urlencode(params)}"

    async def health_check(self) -> HealthCheckResult:
        """Probe GET /accounts/me and return current health status."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="account_id, client_id and client_secret (or access_token) are required",
            )

        # For Server-to-Server, no pre-existing access_token is required —
        # the client will exchange on demand.  For legacy flow we need one.
        if not self._account_id and not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="OAuth flow not completed — access_token is missing",
            )

        client = self._make_client()
        try:
            # For S2S: get_token is called implicitly inside _auth_header
            await with_retry(client.get_account_info)
            await client.aclose()
            self._circuit_breaker.on_success()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Zoom API is reachable",
            )
        except ZoomAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ZoomNetworkError as exc:
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

    # ── Users ────────────────────────────────────────────────────────────────

    async def list_users(
        self,
        status: str = "active",
        page_size: int = 300,
    ) -> dict[str, Any]:
        """Return users from the Zoom account."""
        client = self._ensure_client()
        return await with_retry(
            client.get_users,
            status=status,
            page_size=page_size,
        )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """
        Sync Zoom users, meetings, and recordings into the knowledge base.

        full=True  → fetch all records (paginated).
        since      → passed as from_date to the recordings endpoint.
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        from_date = since.strftime("%Y-%m-%d") if since else ""

        found = 0
        synced = 0
        failed = 0

        # Sync meetings
        try:
            meetings = await self._fetch_all_meetings()
        except ZoomError as exc:
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

        # Sync recordings
        try:
            recordings = await self._fetch_all_recordings(from_date=from_date)
        except ZoomError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        found += len(recordings)
        for record in recordings:
            try:
                doc = normalize_recording(record, self.connector_id, self.tenant_id)
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

    async def _fetch_all_meetings(
        self, user_id: str = "me", meeting_type: str = "scheduled"
    ) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        next_page_token: str = ""
        while True:
            page = await with_retry(
                self.http_client.get_meetings,
                user_id=user_id,
                type=meeting_type,
                page_size=SYNC_PAGE_SIZE,
                next_page_token=next_page_token,
            )
            records.extend(page.get("meetings", []))
            next_page_token = page.get("next_page_token", "")
            if not next_page_token:
                break
        return records

    async def _fetch_all_recordings(
        self, user_id: str = "me", from_date: str = "", to_date: str = ""
    ) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        next_page_token: str = ""
        while True:
            page = await with_retry(
                self.http_client.get_recordings,
                user_id=user_id,
                from_date=from_date,
                to_date=to_date,
                page_size=SYNC_PAGE_SIZE,
                next_page_token=next_page_token,
            )
            records.extend(page.get("meetings", []))
            next_page_token = page.get("next_page_token", "")
            if not next_page_token:
                break
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Meetings ─────────────────────────────────────────────────────────────

    async def list_meetings(
        self,
        user_id: str = "me",
        meeting_type: str = "scheduled",
        page_size: int = 300,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(
            client.get_meetings,
            user_id=user_id,
            type=meeting_type,
            page_size=page_size,
        )

    async def get_meeting(self, meeting_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_meeting, meeting_id)

    # ── Recordings ───────────────────────────────────────────────────────────

    async def list_recordings(
        self,
        user_id: str = "me",
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(
            client.get_recordings,
            user_id=user_id,
            from_date=from_date or "",
            to_date=to_date or "",
        )

    async def get_recording(self, meeting_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_recording, meeting_id)

    # ── Webinars ─────────────────────────────────────────────────────────────

    async def list_webinars(self, user_id: str = "me") -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_webinars, user_id=user_id)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> ZoomHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> ZoomConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
