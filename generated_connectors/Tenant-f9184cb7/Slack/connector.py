"""Slack connector — orchestration only.

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
        SyncResult,
        SyncStatus,
    )
    _BASE = BaseConnector
    _HAS_SDK = True
except ImportError:
    _BASE = object  # type: ignore[assignment,misc]
    _HAS_SDK = False

from client.http_client import SlackHTTPClient
from exceptions import SlackAuthError, SlackError, SlackNetworkError
from helpers.utils import normalize_message, with_retry
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

_SLACK_API_BASE = "https://slack.com/api"
_SYNC_WINDOW_DAYS = 30
_DEFAULT_CHANNEL_TYPES = "public_channel"


class SlackConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for Slack via the Slack Web API."""

    CONNECTOR_TYPE = "slack"
    CONNECTOR_NAME = "Slack"
    AUTH_TYPE = "oauth2"

    REQUIRED_SCOPES: List[str] = [
        "channels:read",
        "channels:history",
        "users:read",
        "groups:read",
        "groups:history",
    ]

    REQUIRED_CONFIG_KEYS = ["bot_token"]

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
        self._http_client: Optional[SlackHTTPClient] = None

    def _ensure_client(self) -> SlackHTTPClient:
        if self._http_client is None:
            self._http_client = SlackHTTPClient(base_url=_SLACK_API_BASE)
        return self._http_client

    def _get_token(self) -> str:
        return self.config.get("bot_token", "")

    def _get_channel_types(self) -> str:
        return self.config.get("channel_types") or _DEFAULT_CHANNEL_TYPES

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        bot_token = self.config.get("bot_token")

        if not bot_token:
            logger.warning("slack.install.missing_credentials", connector_id=self.connector_id)
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,  # type: ignore[name-defined]
                    auth_status=AuthStatus.MISSING_CREDENTIALS,  # type: ignore[name-defined]
                    message="bot_token is required",
                )
            return InstallResult(
                health=_LocalConnectorHealth.OFFLINE,
                auth_status=_LocalAuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="bot_token is required",
            )

        logger.info("slack.install.ok", connector_id=self.connector_id)

        if _HAS_SDK:
            return ConnectorStatus(  # type: ignore[name-defined]
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                message="Connector installed — bot token present",
            )
        return InstallResult(
            health=_LocalConnectorHealth.HEALTHY,
            auth_status=_LocalAuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Connector installed — bot token present",
        )

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        token = self._get_token()
        try:
            data = await with_retry(
                lambda: self._ensure_client().get_auth_test(token),
                max_attempts=2,
            )
            workspace = data.get("team", "unknown workspace")
            msg = f"Connected to workspace: {workspace}"

            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                    auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                    message=msg,
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.HEALTHY,
                auth_status=_LocalAuthStatus.CONNECTED,
                message=msg,
            )
        except SlackAuthError as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.INVALID_CREDENTIALS,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.FAILED,  # type: ignore[name-defined]
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
    ) -> Any:
        """Sync messages from public channels.

        Fetches all channels, then for each channel fetches message history.
        Default window: last 30 days (unless since is provided).
        """
        token = self._get_token()
        channel_types = self._get_channel_types()

        if since is None and not full:
            since = datetime.now(timezone.utc) - timedelta(days=_SYNC_WINDOW_DAYS)

        # Convert since to Unix timestamp for Slack's oldest param
        oldest: Optional[str] = None
        if since is not None:
            oldest = str(since.timestamp())

        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            channels = await self.list_channels(types=channel_types)

            for channel in channels:
                channel_id = channel.get("id", "")
                channel_name = channel.get("name", channel_id)
                if not channel_id:
                    continue

                try:
                    messages = await self.list_messages(
                        channel_id=channel_id,
                        oldest=oldest,
                    )
                    documents_found += len(messages)

                    for message in messages:
                        try:
                            doc = normalize_message(
                                message,
                                channel_id,
                                channel_name,
                                self.connector_id,
                                self.tenant_id,
                            )
                            if _HAS_SDK:
                                normalized = NormalizedDocument(  # type: ignore[name-defined]
                                    id=doc.id,
                                    source_id=f"{channel_id}:{message.get('ts', '')}",
                                    title=doc.title,
                                    content=doc.content,
                                    content_type="text",
                                    source_url="",
                                    author=message.get("user", ""),
                                    source="slack",
                                    tenant_id=self.tenant_id,
                                    connector_id=self.connector_id,
                                    metadata=doc.metadata,
                                )
                                await self.ingest_document(normalized, kb_id=kb_id or "")
                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "slack.sync.message_failed",
                                channel_id=channel_id,
                                ts=message.get("ts", ""),
                                error=str(exc),
                            )
                            documents_failed += 1

                except SlackAuthError:
                    raise
                except Exception as exc:
                    logger.error(
                        "slack.sync.channel_failed",
                        channel_id=channel_id,
                        error=str(exc),
                    )
                    documents_failed += 1

            status = _LocalSyncStatus.COMPLETED if documents_failed == 0 else _LocalSyncStatus.PARTIAL
            msg = f"Synced {documents_synced}/{documents_found} messages from {len(channels)} channels"

            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,  # type: ignore[name-defined]
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
            logger.error("slack.sync.failed", error=str(exc), connector_id=self.connector_id)
            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.FAILED,  # type: ignore[name-defined]
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

    # ── convenience methods ───────────────────────────────────────────────────

    async def list_channels(self, types: str = _DEFAULT_CHANNEL_TYPES) -> List[Dict[str, Any]]:
        """List all channels via paginated conversations.list."""
        token = self._get_token()
        channels: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda c=cursor: self._ensure_client().get_conversations_list(
                    token, types=types, cursor=c
                ),
                max_attempts=3,
            )
            channels.extend(resp.get("channels", []))
            next_cursor = (resp.get("response_metadata") or {}).get("next_cursor", "")
            if not next_cursor:
                break
            cursor = next_cursor

        return channels

    async def list_messages(
        self,
        channel_id: str,
        oldest: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List messages in a channel via paginated conversations.history."""
        token = self._get_token()
        messages: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda c=cursor: self._ensure_client().get_conversations_history(
                    token, channel_id=channel_id, cursor=c, limit=limit, oldest=oldest
                ),
                max_attempts=3,
            )
            messages.extend(resp.get("messages", []))
            next_cursor = (resp.get("response_metadata") or {}).get("next_cursor", "")
            if not next_cursor:
                break
            cursor = next_cursor

        return messages

    async def list_users(self) -> List[Dict[str, Any]]:
        """List all workspace members via paginated users.list."""
        token = self._get_token()
        users: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda c=cursor: self._ensure_client().get_users_list(token, cursor=c),
                max_attempts=3,
            )
            users.extend(resp.get("members", []))
            next_cursor = (resp.get("response_metadata") or {}).get("next_cursor", "")
            if not next_cursor:
                break
            cursor = next_cursor

        return users

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """Fetch a single user by ID via users.info."""
        token = self._get_token()
        resp = await with_retry(
            lambda: self._ensure_client().get_user_info(token, user_id=user_id),
            max_attempts=3,
        )
        return resp.get("user", {})

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> "SlackConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
