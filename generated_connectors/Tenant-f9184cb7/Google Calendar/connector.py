"""Google Calendar connector — orchestration only.

All HTTP calls  → client/http_client.py
All normalization + retry → helpers/utils.py
All models (standalone) → models.py

Imports BaseConnector via a try/except guard so this module loads cleanly
in the gateway's AST sandbox even when the Shielva SDK is absent.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog

try:
    from shielva_connectors.base import BaseConnector
    _BASE = BaseConnector
    _HAS_SDK = True
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: Optional[Dict[str, Any]] = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

    _BASE = BaseConnector  # type: ignore[assignment,misc]
    _HAS_SDK = False

# --- secondary try-block for the richer shared SDK (present inside gateway) ---
try:
    from shared.base_connector import (
        AuthStatus as _SDKAuthStatus,
        ConnectorHealth as _SDKConnectorHealth,
        ConnectorStatus,
        NormalizedDocument,
        RefreshError,
        SyncResult as _SDKSyncResult,
        SyncStatus as _SDKSyncStatus,
        TokenInfo,
    )
    _HAS_SHARED_SDK = True
except ImportError:
    _HAS_SHARED_SDK = False

from client.http_client import GoogleCalendarHTTPClient
from exceptions import (
    GoogleCalendarAuthError,
    GoogleCalendarError,
    GoogleCalendarNetworkError,
)
from helpers.utils import normalize_event, with_retry
from models import (
    AuthStatus as _LocalAuthStatus,
    ConnectorHealth as _LocalConnectorHealth,
    ConnectorDocument,
    InstallResult,
    HealthCheckResult,
    SyncResult as _LocalSyncResult,
    SyncStatus as _LocalSyncStatus,
)

logger = structlog.get_logger(__name__)

_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"

# Scopes required by this connector
_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
]

# Default sync window: events in the next 30 days
_SYNC_WINDOW_DAYS = 30


class GoogleCalendarConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for the Google Calendar API (v3)."""

    CONNECTOR_TYPE = "google_calendar"
    CONNECTOR_NAME = "Google Calendar"
    AUTH_TYPE = "oauth2"
    AUTH_URI = _AUTH_URI
    TOKEN_URI = _TOKEN_URI

    REQUIRED_SCOPES: List[str] = _SCOPES

    REQUIRED_CONFIG_KEYS = [
        "client_id",
        "client_secret",
    ]

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        if _HAS_SDK or _HAS_SHARED_SDK:
            super().__init__(tenant_id, connector_id, cfg)
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = cfg
            self._token_info: Optional[Any] = None

        self._http_client: Optional[GoogleCalendarHTTPClient] = None

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> GoogleCalendarHTTPClient:
        """Return (and lazily create) the HTTP client."""
        if self._http_client is None:
            self._http_client = GoogleCalendarHTTPClient(base_url=_CALENDAR_BASE)
        return self._http_client

    async def _get_valid_token(self) -> str:
        """Return a valid access token, refreshing if the SDK is available."""
        if _HAS_SHARED_SDK:
            try:
                token_info = await self.ensure_token()  # type: ignore[attr-defined]
                return token_info.access_token
            except Exception:
                pass
        if hasattr(self, "_token_info") and self._token_info:
            if isinstance(self._token_info, dict):
                return self._token_info.get("access_token", "")
            return getattr(self._token_info, "access_token", "")
        return ""

    # ── SDK hook: token refresh ───────────────────────────────────────────────

    if _HAS_SHARED_SDK:
        async def on_token_refresh(self) -> Any:  # type: ignore[override]
            """Refresh the OAuth2 access token using the stored refresh token."""
            if not self._token_info or not self._token_info.refresh_token:  # type: ignore[attr-defined]
                raise RefreshError("No refresh token available")  # type: ignore[name-defined]

            client_id = self.config.get("client_id", "")
            client_secret = self.config.get("client_secret", "")
            stored_token = self._token_info.refresh_token  # type: ignore[attr-defined]

            data = await self._ensure_client().refresh_access_token(
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=stored_token,
            )

            expires_in = int(data.get("expires_in", 3600))
            new_scopes = (
                data.get("scope", "").split()
                if data.get("scope")
                else list(self._token_info.scopes)  # type: ignore[attr-defined]
            )
            return TokenInfo(  # type: ignore[name-defined]
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token") or stored_token,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                token_type=data.get("token_type", "Bearer"),
                scopes=new_scopes,
            )

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        """Validate install-time config and return connector/install status.

        Returns InstallResult (standalone) or ConnectorStatus (SDK present).
        Missing client_id or client_secret → MISSING_CREDENTIALS result.
        """
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        if not client_id or not client_secret:
            logger.warning(
                "google_calendar.install.missing_credentials",
                connector_id=self.connector_id,
            )
            if _HAS_SHARED_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=_SDKConnectorHealth.OFFLINE,  # type: ignore[name-defined]
                    auth_status=_SDKAuthStatus.MISSING_CREDENTIALS,  # type: ignore[name-defined]
                    message="client_id and client_secret are required",
                )
            return InstallResult(
                health=_LocalConnectorHealth.OFFLINE,
                auth_status=_LocalAuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="client_id and client_secret are required",
            )

        if _HAS_SHARED_SDK:
            await self.save_config(  # type: ignore[attr-defined]
                {"client_id": client_id, "client_secret": client_secret}
            )

        logger.info("google_calendar.install.ok", connector_id=self.connector_id)

        if _HAS_SHARED_SDK:
            return ConnectorStatus(  # type: ignore[name-defined]
                connector_id=self.connector_id,
                health=_SDKConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                auth_status=_SDKAuthStatus.PENDING,  # type: ignore[name-defined]
                message="Connector installed — complete OAuth to connect",
            )
        return InstallResult(
            health=_LocalConnectorHealth.HEALTHY,
            auth_status=_LocalAuthStatus.PENDING,
            connector_id=self.connector_id,
            message="Connector installed — complete OAuth to connect",
        )

    # ── authorize ─────────────────────────────────────────────────────────────

    async def authorize(self) -> str:
        """Return a Google OAuth2 authorization URL.

        The URL includes offline access_type so the server receives a refresh
        token. The caller should redirect the user to this URL.
        """
        client_id = self.config.get("client_id", "")
        redirect_uri = self.config.get(
            "redirect_uri", "https://localhost:8000/connectors/oauth/callback"
        )
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        }
        url = f"{_AUTH_URI}?{urllib.parse.urlencode(params)}"
        logger.info(
            "google_calendar.authorize.url_built",
            connector_id=self.connector_id,
        )
        return url

    async def exchange_code(self, auth_code: str, state: Optional[str] = None) -> Any:
        """Exchange an OAuth2 authorization code for access + refresh tokens.

        Separate from authorize() which just builds the redirect URL.
        Only available when the Shielva SDK is present.
        """
        if not _HAS_SHARED_SDK:
            raise NotImplementedError("exchange_code requires the Shielva SDK")

        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")
        redirect_uri = self.config.get("redirect_uri", "")

        data = await self._ensure_client().exchange_code_for_token(
            client_id=client_id,
            client_secret=client_secret,
            code=auth_code,
            redirect_uri=redirect_uri,
        )

        expires_in = int(data.get("expires_in", 3600))
        scopes = (
            data.get("scope", "").split()
            if data.get("scope")
            else list(self.REQUIRED_SCOPES)
        )
        token_info = TokenInfo(  # type: ignore[name-defined]
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )
        await self.set_token(token_info)  # type: ignore[attr-defined]
        logger.info("google_calendar.exchange_code.ok", connector_id=self.connector_id)
        return token_info

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        """Check Calendar API connectivity by fetching user info.

        Returns ConnectorStatus (SDK) or HealthCheckResult (standalone).
        Calls GET /oauth2/v2/userinfo as the health probe.
        """
        try:
            access_token = await self._get_valid_token()
            await with_retry(
                lambda: self._ensure_client().get_user_info(access_token),
                max_retries=2,
            )
            if _HAS_SHARED_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=_SDKConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                    auth_status=_SDKAuthStatus.CONNECTED,  # type: ignore[name-defined]
                    message="Google Calendar API reachable",
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.HEALTHY,
                auth_status=_LocalAuthStatus.CONNECTED,
                message="Google Calendar API reachable",
            )
        except GoogleCalendarAuthError:
            if _HAS_SHARED_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=_SDKConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=_SDKAuthStatus.TOKEN_EXPIRED,  # type: ignore[name-defined]
                    message="Token expired — re-authorize the connector",
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.TOKEN_EXPIRED,
                message="Token expired — re-authorize the connector",
            )
        except GoogleCalendarError as exc:
            if _HAS_SHARED_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=_SDKConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=_SDKAuthStatus.CONNECTED,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.FAILED,
                message=str(exc),
            )

    # ── sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: Optional[datetime] = None,
        kb_id: str = "",
        webhook_url: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """Sync upcoming events from the primary calendar into the knowledge base.

        Lists events from now to +30 days. Calls ingest_document (SDK) or
        accumulates ConnectorDocuments (standalone). Returns SyncResult.
        """
        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=_SYNC_WINDOW_DAYS)).isoformat()

        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            access_token = await self._get_valid_token()
            page_token: Optional[str] = None

            while True:
                resp = await with_retry(
                    lambda pt=page_token: self._ensure_client().get_events(
                        access_token,
                        calendar_id="primary",
                        time_min=time_min,
                        time_max=time_max,
                        max_results=100,
                        page_token=pt,
                    ),
                    max_retries=3,
                )

                events: List[Dict[str, Any]] = resp.get("items", [])
                documents_found += len(events)

                for event in events:
                    try:
                        doc = normalize_event(event, self.connector_id, self.tenant_id)
                        if _HAS_SHARED_SDK:
                            normalized = NormalizedDocument(  # type: ignore[name-defined]
                                id=doc.id,
                                source_id=event.get("id", ""),
                                title=doc.title,
                                content=doc.content,
                                content_type="text",
                                source_url=doc.metadata.get("html_link", ""),
                                author=doc.metadata.get("organizer_email", ""),
                                source="google_calendar",
                                tenant_id=self.tenant_id,
                                connector_id=self.connector_id,
                                metadata=doc.metadata,
                            )
                            await self.ingest_document(  # type: ignore[attr-defined]
                                normalized,
                                kb_id=kb_id or "",
                                webhook_url=webhook_url,
                            )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "google_calendar.sync.event_failed",
                            event_id=event.get("id", ""),
                            error=str(exc),
                        )
                        documents_failed += 1

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

            status = (
                _LocalSyncStatus.COMPLETED
                if documents_failed == 0
                else _LocalSyncStatus.PARTIAL
            )
            msg = f"Synced {documents_synced}/{documents_found} events"

            if _HAS_SHARED_SDK:
                return _SDKSyncResult(  # type: ignore[name-defined]
                    status=_SDKSyncStatus.COMPLETED if documents_failed == 0 else _SDKSyncStatus.PARTIAL,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=msg,
                )
            return _LocalSyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=msg,
            )

        except Exception as exc:
            logger.error(
                "google_calendar.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            if _HAS_SHARED_SDK:
                return _SDKSyncResult(  # type: ignore[name-defined]
                    status=_SDKSyncStatus.FAILED,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=str(exc),
                )
            return _LocalSyncResult(
                status=_LocalSyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public convenience methods ────────────────────────────────────────────

    async def list_calendars(self) -> Dict[str, Any]:
        """List all calendars for the authenticated user.

        Returns the raw API response: {kind, etag, items: [...]}.
        """
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self._ensure_client().get_calendar_list(access_token),
            max_retries=3,
        )

    async def list_events(
        self,
        calendar_id: str = "primary",
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        max_results: int = 100,
    ) -> Dict[str, Any]:
        """List events in a calendar.

        Returns the raw API response: {kind, summary, items: [...], nextPageToken?}.
        """
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self._ensure_client().get_events(
                access_token,
                calendar_id=calendar_id,
                time_min=time_min,
                time_max=time_max,
                max_results=max_results,
            ),
            max_retries=3,
        )

    async def get_event(
        self,
        event_id: str,
        calendar_id: str = "primary",
    ) -> Dict[str, Any]:
        """Fetch a single event by event_id (and optional calendar_id).

        Returns the raw Google Calendar API event object.
        """
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self._ensure_client().get_event(access_token, calendar_id, event_id),
            max_retries=3,
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release any held async resources."""
        self._http_client = None

    async def __aenter__(self) -> "GoogleCalendarConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
