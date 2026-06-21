"""Discord connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: bot token (Discord API v10). The bot token is treated as the api_key
(public ``REQUIRED_CONFIG_KEYS = ["bot_token"]``, ``AUTH_TYPE = "api_key"``)
and sent as ``Authorization: Bot <bot_token>``. An optional ``oauth_token``
override switches the header to ``Authorization: Bearer <oauth_token>`` for
tenants that prefer the OAuth2 user-token path.
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

from client.http_client import DISCORD_BASE_URL, DiscordHTTPClient
from exceptions import (
    DiscordAuthError,
    DiscordError,
    DiscordNetworkError,
    DiscordNotFound,
)
from helpers.normalizer import normalize_guild, normalize_message
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)


class DiscordConnector(BaseConnector):
    """Shielva connector for the Discord REST API v10 (Users, Guilds,
    Channels, Messages, Members, Roles, Webhooks)."""

    CONNECTOR_TYPE = "discord"
    CONNECTOR_NAME = "Discord"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "bot_token",
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
        self.bot_token: str = self.config.get("bot_token", "") or ""
        self.oauth_token: str = self.config.get("oauth_token", "") or ""
        self.base_url: str = self.config.get("base_url", "") or DISCORD_BASE_URL
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 50)

        self.http_client = DiscordHTTPClient(
            bot_token=self.bot_token,
            oauth_token=self.oauth_token,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Discord install only requires ``bot_token`` (or, equivalently, an
        ``oauth_token`` override). ``base_url`` defaults to v10.
        """
        bot_token = self.config.get("bot_token", "") or ""
        oauth_token = self.config.get("oauth_token", "") or ""

        if not bot_token and not oauth_token:
            logger.warning(
                "discord.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="bot_token (or oauth_token) is required",
            )

        await self.save_config(
            {
                "bot_token": bot_token,
                "oauth_token": oauth_token,
                "base_url": self.config.get("base_url", DISCORD_BASE_URL),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 50),
            }
        )
        logger.info("discord.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Discord connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured bot_token (or
        oauth_token override when set).
        """
        token = self.oauth_token or self.bot_token
        token_type = "Bearer" if self.oauth_token else "Bot"
        return TokenInfo(
            access_token=token,
            refresh_token=None,
            expires_at=None,
            token_type=token_type,
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Discord API connectivity by calling ``GET /users/@me``."""
        try:
            await with_retry(
                lambda: self.http_client.get_current_user(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Discord API reachable",
            )
        except DiscordAuthError as exc:
            # 401 → token expired, 403 → invalid credentials
            if getattr(exc, "status_code", 0) == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Discord auth forbidden: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Discord auth failed: {exc}",
            )
        except DiscordNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Discord network error: {exc}",
            )
        except DiscordError as exc:
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
        """Sync Discord guilds + messages into the Shielva KB.

        For each guild the bot/user belongs to, iterate text channels and
        pull the most recent page of messages. Normalize each as a
        NormalizedDocument and ingest.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            guilds_resp = await with_retry(
                lambda: self.http_client.list_guilds(limit=100),
                max_retries=3,
            )
            guilds = guilds_resp if isinstance(guilds_resp, list) else guilds_resp.get("guilds", [])

            for guild in guilds or []:
                guild_id = str(guild.get("id", ""))
                if not guild_id:
                    continue

                # Ingest the guild itself as a doc
                documents_found += 1
                try:
                    doc = normalize_guild(guild, self.connector_id, self.tenant_id)
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error("discord.sync.guild_failed", error=str(exc))
                    documents_failed += 1

                # Walk channels → messages
                try:
                    channels_resp = await with_retry(
                        lambda gid=guild_id: self.http_client.list_channels(gid),
                        max_retries=3,
                    )
                    channels = (
                        channels_resp
                        if isinstance(channels_resp, list)
                        else channels_resp.get("channels", [])
                    )
                except Exception as exc:
                    logger.error(
                        "discord.sync.list_channels_failed",
                        guild_id=guild_id,
                        error=str(exc),
                    )
                    continue

                for channel in channels or []:
                    # Only text-ish channels (0 GUILD_TEXT, 5 ANNOUNCEMENT, 10/11/12 threads)
                    if int(channel.get("type", 0)) not in (0, 5, 10, 11, 12):
                        continue
                    channel_id = str(channel.get("id", ""))
                    if not channel_id:
                        continue
                    try:
                        msgs_resp = await with_retry(
                            lambda cid=channel_id: self.http_client.list_messages(
                                cid, limit=50,
                            ),
                            max_retries=3,
                        )
                    except Exception as exc:
                        logger.error(
                            "discord.sync.list_messages_failed",
                            channel_id=channel_id,
                            error=str(exc),
                        )
                        continue
                    messages = (
                        msgs_resp
                        if isinstance(msgs_resp, list)
                        else msgs_resp.get("messages", [])
                    )
                    for raw in messages or []:
                        documents_found += 1
                        try:
                            doc = normalize_message(
                                raw, self.connector_id, self.tenant_id,
                            )
                            await self.ingest_document(
                                doc, kb_id=kb_id or "", webhook_url=webhook_url,
                            )
                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "discord.sync.message_failed", error=str(exc),
                            )
                            documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Discord documents",
            )
        except Exception as exc:
            logger.error("discord.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def list_guilds(
        self,
        limit: int = 100,
        before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> Any:
        """GET /users/@me/guilds — list guilds the bot/user belongs to."""
        return await with_retry(
            lambda: self.http_client.list_guilds(
                limit=limit, before=before, after=after,
            ),
            max_retries=3,
        )

    async def get_guild(self, guild_id: str) -> Dict[str, Any]:
        """GET /guilds/{guild_id}."""
        return await with_retry(
            lambda: self.http_client.get_guild(guild_id),
            max_retries=3,
        )

    async def list_channels(self, guild_id: str) -> Any:
        """GET /guilds/{guild_id}/channels."""
        return await with_retry(
            lambda: self.http_client.list_channels(guild_id),
            max_retries=3,
        )

    async def get_channel(self, channel_id: str) -> Dict[str, Any]:
        """GET /channels/{channel_id}."""
        return await with_retry(
            lambda: self.http_client.get_channel(channel_id),
            max_retries=3,
        )

    async def send_message(
        self,
        channel_id: str,
        content: str,
        embeds: Optional[list] = None,
        components: Optional[list] = None,
    ) -> Dict[str, Any]:
        """POST /channels/{channel_id}/messages."""
        return await self.http_client.send_message(
            channel_id, content, embeds=embeds, components=components,
        )

    async def get_message(self, channel_id: str, message_id: str) -> Dict[str, Any]:
        """GET /channels/{channel_id}/messages/{message_id}."""
        return await with_retry(
            lambda: self.http_client.get_message(channel_id, message_id),
            max_retries=3,
        )

    async def list_messages(
        self,
        channel_id: str,
        limit: int = 50,
        before: Optional[str] = None,
        after: Optional[str] = None,
        around: Optional[str] = None,
    ) -> Any:
        """GET /channels/{channel_id}/messages."""
        return await with_retry(
            lambda: self.http_client.list_messages(
                channel_id, limit=limit, before=before, after=after, around=around,
            ),
            max_retries=3,
        )

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
    ) -> Dict[str, Any]:
        """PATCH /channels/{channel_id}/messages/{message_id}."""
        return await self.http_client.edit_message(channel_id, message_id, content)

    async def delete_message(
        self,
        channel_id: str,
        message_id: str,
    ) -> Dict[str, Any]:
        """DELETE /channels/{channel_id}/messages/{message_id}."""
        return await self.http_client.delete_message(channel_id, message_id)

    async def list_guild_members(
        self,
        guild_id: str,
        limit: int = 100,
        after: Optional[str] = None,
    ) -> Any:
        """GET /guilds/{guild_id}/members."""
        return await with_retry(
            lambda: self.http_client.list_guild_members(
                guild_id, limit=limit, after=after,
            ),
            max_retries=3,
        )

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """GET /users/{user_id}."""
        return await with_retry(
            lambda: self.http_client.get_user(user_id),
            max_retries=3,
        )

    async def add_role(
        self,
        guild_id: str,
        user_id: str,
        role_id: str,
    ) -> Dict[str, Any]:
        """PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id}."""
        return await self.http_client.add_role(guild_id, user_id, role_id)

    async def remove_role(
        self,
        guild_id: str,
        user_id: str,
        role_id: str,
    ) -> Dict[str, Any]:
        """DELETE /guilds/{guild_id}/members/{user_id}/roles/{role_id}."""
        return await self.http_client.remove_role(guild_id, user_id, role_id)

    async def create_webhook(
        self,
        channel_id: str,
        name: str,
    ) -> Dict[str, Any]:
        """POST /channels/{channel_id}/webhooks."""
        return await self.http_client.create_webhook(channel_id, name)

    async def execute_webhook(
        self,
        webhook_id: str,
        webhook_token: str,
        content: str,
        embeds: Optional[list] = None,
    ) -> Dict[str, Any]:
        """POST /webhooks/{webhook_id}/{webhook_token}."""
        return await self.http_client.execute_webhook(
            webhook_id, webhook_token, content, embeds=embeds,
        )
