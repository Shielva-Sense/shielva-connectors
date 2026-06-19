"""Outlook Calendar connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog

try:
    from shared.base_connector import (
        AuthStatus,
        BaseConnector,
        ConnectorHealth,
        ConnectorStatus,
        NormalizedDocument,
        RefreshError,
        SyncResult,
        SyncStatus,
        TokenInfo,
    )
    _BASE = BaseConnector
    _HAS_SDK = True
except ImportError:
    _BASE = object  # type: ignore[assignment,misc]
    _HAS_SDK = False

from client.http_client import OutlookCalendarHTTPClient
from exceptions import OutlookCalendarAuthError, OutlookCalendarError, OutlookCalendarNetworkError
from helpers.utils import normalize_event, with_retry
from models import (
    AuthStatus as _LocalAuthStatus,
    ConnectorHealth as _LocalConnectorHealth,
    InstallResult,
    HealthCheckResult,
    SyncResult as _LocalSyncResult,
    SyncStatus as _LocalSyncStatus,
)

logger = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_AUTH_URI = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_TOKEN_URI = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_SYNC_WINDOW_DAYS = 30


class OutlookCalendarConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for Outlook Calendar via Microsoft Graph API."""

    CONNECTOR_TYPE = "outlook_calendar"
    CONNECTOR_NAME = "Outlook Calendar"
    AUTH_TYPE = "oauth2"
    AUTH_URI = _AUTH_URI
    TOKEN_URI = _TOKEN_URI

    REQUIRED_SCOPES: List[str] = [
        "https://graph.microsoft.com/Calendars.Read",
        "offline_access",
    ]

    REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        if _HAS_SDK:
            super().__init__(tenant_id, connector_id, cfg)
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = cfg
            self._token_info: Optional[Any] = None
        self._http_client: Optional[OutlookCalendarHTTPClient] = None

    def _ensure_client(self) -> OutlookCalendarHTTPClient:
        if self._http_client is None:
            self._http_client = OutlookCalendarHTTPClient(base_url=_GRAPH_BASE)
        return self._http_client

    async def _get_valid_token(self) -> str:
        if _HAS_SDK:
            token_info = await self.ensure_token()
            return token_info.access_token  # type: ignore[return-value]
        if self._token_info:
            return self._token_info.get("access_token", "")  # type: ignore[return-value]
        return ""

    if _HAS_SDK:
        async def on_token_refresh(self) -> "TokenInfo":  # type: ignore[override]
            if not self._token_info or not self._token_info.refresh_token:
                raise RefreshError("No refresh token available")  # type: ignore[name-defined]
            tenant_hint = self.config.get("tenant_hint", "common")
            token_uri = self.config.get("token_url") or f"https://login.microsoftonline.com/{tenant_hint}/oauth2/v2.0/token"
            data = await self._ensure_client().post_form_data(
                url=token_uri,
                payload={
                    "grant_type": "refresh_token",
                    "refresh_token": self._token_info.refresh_token,
                    "client_id": self.config.get("client_id", ""),
                    "client_secret": self.config.get("client_secret", ""),
                    "scope": " ".join(self.REQUIRED_SCOPES),
                },
                context="on_token_refresh",
            )
            expires_in = int(data.get("expires_in", 3600))
            return TokenInfo(  # type: ignore[name-defined]
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token") or self._token_info.refresh_token,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                token_type=data.get("token_type", "Bearer"),
                scopes=data.get("scope", "").split() or list(self._token_info.scopes),
            )

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        if not client_id or not client_secret:
            logger.warning("outlook_calendar.install.missing_credentials", connector_id=self.connector_id)
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,  # type: ignore[name-defined]
                    auth_status=AuthStatus.MISSING_CREDENTIALS,  # type: ignore[name-defined]
                    message="client_id and client_secret are required",
                )
            return InstallResult(
                health=_LocalConnectorHealth.OFFLINE,
                auth_status=_LocalAuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="client_id and client_secret are required",
            )

        if _HAS_SDK:
            await self.save_config({"client_id": client_id, "client_secret": client_secret})

        logger.info("outlook_calendar.install.ok", connector_id=self.connector_id)

        if _HAS_SDK:
            return ConnectorStatus(  # type: ignore[name-defined]
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                auth_status=AuthStatus.PENDING,  # type: ignore[name-defined]
                message="Connector installed — complete OAuth to connect",
            )
        return InstallResult(
            health=_LocalConnectorHealth.HEALTHY,
            auth_status=_LocalAuthStatus.PENDING,
            connector_id=self.connector_id,
            message="Connector installed — complete OAuth to connect",
        )

    # ── authorize ─────────────────────────────────────────────────────────────

    async def authorize(self, auth_code: str, state: Optional[str] = None) -> Any:
        if not _HAS_SDK:
            raise NotImplementedError("authorize requires the Shielva SDK")
        tenant_hint = self.config.get("tenant_hint", "common")
        token_uri = self.config.get("token_url") or f"https://login.microsoftonline.com/{tenant_hint}/oauth2/v2.0/token"
        data = await self._ensure_client().post_form_data(
            url=token_uri,
            payload={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": self.config.get("client_id", ""),
                "client_secret": self.config.get("client_secret", ""),
                "redirect_uri": self.config.get("redirect_uri", ""),
                "scope": " ".join(self.REQUIRED_SCOPES),
            },
            context="authorize",
        )
        expires_in = int(data.get("expires_in", 3600))
        token_info = TokenInfo(  # type: ignore[name-defined]
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=data.get("scope", "").split() or list(self.REQUIRED_SCOPES),
        )
        await self.set_token(token_info)
        logger.info("outlook_calendar.authorize.ok", connector_id=self.connector_id)
        return token_info

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        try:
            access_token = await self._get_valid_token()
            await with_retry(lambda: self._ensure_client().get_me(access_token), max_retries=2)
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                    auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                    message="Microsoft Graph API reachable",
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.HEALTHY,
                auth_status=_LocalAuthStatus.CONNECTED,
                message="Microsoft Graph API reachable",
            )
        except OutlookCalendarAuthError:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.TOKEN_EXPIRED,  # type: ignore[name-defined]
                    message="Token expired — re-authorize",
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.TOKEN_EXPIRED,
                message="Token expired — re-authorize",
            )
        except Exception as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
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
    ) -> Any:
        """Sync upcoming events (next 30 days) via /me/calendarView with pagination."""
        now = datetime.now(timezone.utc)
        start_dt = now.strftime("%Y-%m-%dT%H:%M:%S")
        end_dt = (now + timedelta(days=_SYNC_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")

        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            access_token = await self._get_valid_token()
            next_link: Optional[str] = None
            first_call = True

            while True:
                resp = await with_retry(
                    lambda nl=next_link, fc=first_call: self._ensure_client().get_events(
                        access_token,
                        calendar_id="primary",
                        start_datetime=start_dt if fc else None,
                        end_datetime=end_dt if fc else None,
                        top=100,
                        next_link=nl,
                    ),
                    max_retries=3,
                )
                first_call = False

                events: List[Dict[str, Any]] = resp.get("value", [])
                documents_found += len(events)

                for event in events:
                    try:
                        doc = normalize_event(event, self.connector_id, self.tenant_id)
                        if _HAS_SDK:
                            normalized = NormalizedDocument(  # type: ignore[name-defined]
                                id=doc.id,
                                source_id=event.get("id", ""),
                                title=doc.title,
                                content=doc.content,
                                content_type="text",
                                source_url=doc.metadata.get("web_link", ""),
                                author=doc.metadata.get("organizer_email", ""),
                                source="outlook_calendar",
                                tenant_id=self.tenant_id,
                                connector_id=self.connector_id,
                                metadata=doc.metadata,
                            )
                            await self.ingest_document(normalized, kb_id=kb_id or "", webhook_url=webhook_url)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("outlook_calendar.sync.event_failed", event_id=event.get("id", ""), error=str(exc))
                        documents_failed += 1

                next_link = resp.get("@odata.nextLink")
                if not next_link:
                    break

            status = _LocalSyncStatus.COMPLETED if documents_failed == 0 else _LocalSyncStatus.PARTIAL
            msg = f"Synced {documents_synced}/{documents_found} events"

            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=msg,
                )
            return _LocalSyncResult(status=status, documents_found=documents_found,
                                    documents_synced=documents_synced, documents_failed=documents_failed, message=msg)

        except Exception as exc:
            logger.error("outlook_calendar.sync.failed", error=str(exc), connector_id=self.connector_id)
            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.FAILED,  # type: ignore[name-defined]
                    documents_found=documents_found, documents_synced=documents_synced,
                    documents_failed=documents_failed, message=str(exc),
                )
            return _LocalSyncResult(status=_LocalSyncStatus.FAILED, documents_found=documents_found,
                                    documents_synced=documents_synced, documents_failed=documents_failed, message=str(exc))

    # ── convenience methods ───────────────────────────────────────────────────

    async def list_calendars(self) -> Dict[str, Any]:
        access_token = await self._get_valid_token()
        return await with_retry(lambda: self._ensure_client().get_calendars(access_token), max_retries=3)

    async def list_events(
        self,
        calendar_id: str = "primary",
        start_datetime: Optional[str] = None,
        end_datetime: Optional[str] = None,
        top: int = 100,
    ) -> Dict[str, Any]:
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self._ensure_client().get_events(
                access_token, calendar_id=calendar_id,
                start_datetime=start_datetime, end_datetime=end_datetime, top=top,
            ),
            max_retries=3,
        )

    async def get_event(self, event_id: str, calendar_id: Optional[str] = None) -> Dict[str, Any]:
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self._ensure_client().get_event(access_token, event_id, calendar_id),
            max_retries=3,
        )

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> "OutlookCalendarConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
