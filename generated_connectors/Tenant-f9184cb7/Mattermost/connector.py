"""Mattermost connector — orchestration only.

All HTTP calls    → client/http_client.py
All normalization → helpers/normalizer.py
All utilities     → helpers/utils.py
All error types   → exceptions.py

Authentication model
--------------------
Mattermost issues long-lived **Personal Access Tokens** (or bot tokens). There
is no OAuth refresh cycle, so the token captured at install time is stored
verbatim and used as ``Authorization: Bearer <token>``. ``authorize()`` is
provided as a no-op confirmation step so callers can mirror the lifecycle they
use for OAuth connectors.

Tenant model
------------
Unlike SaaS APIs with a single base URL, every Mattermost deployment has its
own ``{server_url}/api/v4`` host. The base URL is therefore a **per-tenant
install field**, never hardcoded.
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

from client.http_client import MattermostHTTPClient
from exceptions import (
    MattermostAuthError,
    MattermostError,
    MattermostNetworkError,
    MattermostNotFound,
)
from helpers.normalizer import normalize_channel, normalize_post, normalize_user
from helpers.utils import normalize_server_url, with_retry

logger = structlog.get_logger(__name__)


class MattermostConnector(BaseConnector):
    """Shielva connector for self-hosted / cloud Mattermost (REST v4)."""

    CONNECTOR_TYPE = "mattermost"
    CONNECTOR_NAME = "Mattermost"
    AUTH_TYPE = "api_key"
    VERSION = "1.0.0"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "server_url",
        "personal_access_token",
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
        self.server_url: str = normalize_server_url(self.config.get("server_url", ""))
        self.personal_access_token: str = self.config.get("personal_access_token", "")
        self.default_team_id: str = self.config.get("default_team_id", "")
        self.rate_limit_per_min: int = int(self.config.get("rate_limit_per_min") or 200)

        self.http_client = MattermostHTTPClient(
            server_url=self.server_url,
            access_token=self.personal_access_token,
        )

    # ── Internal helpers ────────────────────────────────────────────────────

    def _rebuild_http_client(self) -> None:
        """Recreate the HTTP client after a server_url / token change."""
        self.http_client = MattermostHTTPClient(
            server_url=self.server_url,
            access_token=self.personal_access_token,
        )

    def _ok_status(self, message: str) -> ConnectorStatus:
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message=message,
        )

    def _missing_creds(self) -> ConnectorStatus:
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.MISSING_CREDENTIALS,
            message="server_url and personal_access_token are required",
        )

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and probe ``/users/me`` once."""
        server_url = normalize_server_url(self.config.get("server_url", ""))
        token = self.config.get("personal_access_token", "")
        if not server_url or not token:
            logger.warning(
                "mattermost.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return self._missing_creds()

        # Re-sync state from possibly-updated config
        self.server_url = server_url
        self.personal_access_token = token
        self._rebuild_http_client()

        try:
            user = await self.http_client.get_current_user()
        except MattermostAuthError as exc:
            logger.warning("mattermost.install.unauthorized", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Token rejected by Mattermost: {exc}",
            )
        except (MattermostNetworkError, MattermostError) as exc:
            logger.warning("mattermost.install.network_error", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=f"Could not reach Mattermost: {exc}",
            )

        await self.save_config(
            {
                "server_url": self.server_url,
                "personal_access_token": self.personal_access_token,
                "default_team_id": self.default_team_id,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        # Persist the token so downstream calls see it as the active credential.
        await self.set_token(
            TokenInfo(
                access_token=self.personal_access_token,
                refresh_token=None,
                expires_at=None,
                token_type="Bearer",
                scopes=[],
                metadata={"user_id": user.get("id", "")},
            )
        )
        logger.info(
            "mattermost.install.ok",
            connector_id=self.connector_id,
            user_id=user.get("id", ""),
        )
        return self._ok_status(f"Connected to Mattermost as {user.get('username', '')}")

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """API-key connectors have no OAuth code exchange.

        This method exists for lifecycle parity with OAuth connectors: it
        verifies the stored token by calling ``/users/me`` and returns a
        ``TokenInfo`` describing the active credential.
        """
        if not self.personal_access_token:
            raise MattermostAuthError("No personal_access_token configured")

        user = await self.http_client.get_current_user()
        token_info = TokenInfo(
            access_token=self.personal_access_token,
            refresh_token=None,
            expires_at=None,
            token_type="Bearer",
            scopes=[],
            metadata={"user_id": user.get("id", "")},
        )
        await self.set_token(token_info)
        logger.info("mattermost.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Probe ``/system/ping`` to confirm the server is reachable.

        ``/system/ping`` is the canonical Mattermost liveness probe — it
        returns ``{"status": "OK"}`` and does not require authentication on
        most server configurations. The connector still sends the auth
        header so a permission-policy that locks ``/system/*`` behind auth
        surfaces as ``TOKEN_EXPIRED`` instead of a generic 404.
        """
        try:
            await with_retry(self.http_client.ping, max_retries=2)
        except MattermostAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=str(exc),
            )
        except MattermostNotFound as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=f"Endpoint not found — check server_url: {exc}",
            )
        except (MattermostNetworkError, MattermostError) as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        return self._ok_status("Mattermost API reachable")

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Sync Mattermost teams, channels, and recent channel posts into the KB.

        For each accessible team, list channels; for each channel, fetch the
        most recent page of posts; normalize each into a ``NormalizedDocument``
        (id = ``f"{tenant_id}_{source_id}"``) and ingest. Failures on a single
        document never abort the whole sync — they increment the failed
        counter and the loop continues.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            teams = await self.http_client.list_teams(page=0, per_page=200)
            team_ids: List[str] = [
                t["id"] for t in (teams or []) if isinstance(t, dict) and "id" in t
            ]

            for tid in team_ids:
                channels = await self.http_client.list_channels(
                    team_id=tid, page=0, per_page=200,
                )
                if not isinstance(channels, list):
                    continue

                for ch in channels:
                    documents_found += 1
                    try:
                        ch_doc = normalize_channel(ch, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            ch_doc,
                            kb_id=kb_id or "",
                            webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("mattermost.sync.channel_failed", error=str(exc))
                        documents_failed += 1

                    # Pull a single page of recent posts per channel.
                    try:
                        posts_resp = await self.http_client.list_channel_posts(
                            channel_id=ch.get("id", ""),
                            page=0,
                            per_page=60,
                        )
                    except Exception as exc:
                        logger.error(
                            "mattermost.sync.channel_posts_failed",
                            channel_id=ch.get("id", ""),
                            error=str(exc),
                        )
                        continue

                    posts_map: Dict[str, Any] = (posts_resp or {}).get("posts", {}) or {}
                    for raw in posts_map.values():
                        documents_found += 1
                        try:
                            doc = normalize_post(raw, self.connector_id, self.tenant_id)
                            await self.ingest_document(
                                doc,
                                kb_id=kb_id or "",
                                webhook_url=webhook_url,
                            )
                            documents_synced += 1
                        except Exception as exc:
                            logger.error("mattermost.sync.post_failed", error=str(exc))
                            documents_failed += 1

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED
                    if documents_failed == 0
                    else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=(
                    f"Synced {documents_synced}/{documents_found} Mattermost documents"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — surface as failed sync, not raise
            logger.error("mattermost.sync.failed", error=str(exc))
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ──────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /users/me — fetch the authenticated user."""
        return await self.http_client.get_current_user()

    # Alias mirroring the spec name `get_me`.
    async def get_me(self) -> Dict[str, Any]:
        """GET /users/me — alias of ``get_current_user``."""
        return await self.http_client.get_current_user()

    async def list_users(
        self,
        page: int = 0,
        per_page: int = 60,
        in_team_id: Optional[str] = None,
        in_channel_id: Optional[str] = None,
    ) -> Any:
        """GET /users — list users (optionally scoped to a team or channel)."""
        return await self.http_client.list_users(
            page=page,
            per_page=per_page,
            in_team_id=in_team_id,
            in_channel_id=in_channel_id,
        )

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """GET /users/{user_id}."""
        return await self.http_client.get_user(user_id)

    async def create_user(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /users — admin-only registration."""
        return await self.http_client.create_user(payload)

    async def search_users(
        self,
        team_id: str,
        term: str,
        in_channel_id: Optional[str] = None,
        not_in_channel_id: Optional[str] = None,
    ) -> Any:
        """POST /users/search."""
        return await self.http_client.search_users(
            team_id=team_id,
            term=term,
            in_channel_id=in_channel_id,
            not_in_channel_id=not_in_channel_id,
        )

    async def list_teams(
        self,
        page: int = 0,
        per_page: int = 60,
        include_total_count: bool = False,
    ) -> Any:
        """GET /teams — list teams the token has access to."""
        return await self.http_client.list_teams(
            page=page,
            per_page=per_page,
            include_total_count=include_total_count,
        )

    async def get_team(self, team_id: str) -> Dict[str, Any]:
        """GET /teams/{id}."""
        return await self.http_client.get_team(team_id)

    async def list_channels(
        self,
        team_id: str,
        page: int = 0,
        per_page: int = 60,
        include_deleted: bool = False,
    ) -> Any:
        """GET /teams/{id}/channels."""
        return await self.http_client.list_channels(
            team_id=team_id,
            page=page,
            per_page=per_page,
            include_deleted=include_deleted,
        )

    async def get_channel(self, channel_id: str) -> Dict[str, Any]:
        """GET /channels/{id}."""
        return await self.http_client.get_channel(channel_id)

    async def create_channel(
        self,
        team_id: str,
        name: str,
        display_name: str,
        type: str = "O",
        purpose: str = "",
        header: str = "",
    ) -> Dict[str, Any]:
        """POST /channels.

        ``type`` is ``"O"`` (open / public) or ``"P"`` (private). Any other
        value will be rejected by the server with a 400.
        """
        if type not in ("O", "P"):
            raise ValueError("channel type must be 'O' (open) or 'P' (private)")
        return await self.http_client.create_channel(
            team_id=team_id,
            name=name,
            display_name=display_name,
            type=type,
            purpose=purpose,
            header=header,
        )

    async def delete_channel(self, channel_id: str) -> Dict[str, Any]:
        """DELETE /channels/{id} (soft-delete on the Mattermost server)."""
        return await self.http_client.delete_channel(channel_id)

    async def add_user_to_channel(self, channel_id: str, user_id: str) -> Dict[str, Any]:
        """POST /channels/{id}/members."""
        return await self.http_client.add_user_to_channel(channel_id, user_id)

    async def post_message(
        self,
        channel_id: str,
        message: str,
        root_id: Optional[str] = None,
        props: Optional[Dict[str, Any]] = None,
        file_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /posts — create a new post (or threaded reply via ``root_id``)."""
        return await self.http_client.post_message(
            channel_id=channel_id,
            message=message,
            root_id=root_id,
            props=props,
            file_ids=file_ids,
        )

    async def get_post(self, post_id: str) -> Dict[str, Any]:
        """GET /posts/{id}."""
        return await self.http_client.get_post(post_id)

    async def update_post(
        self,
        post_id: str,
        message: Optional[str] = None,
        props: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """PUT /posts/{id} — partial update of a post body or props."""
        return await self.http_client.update_post(
            post_id=post_id,
            message=message,
            props=props,
        )

    async def delete_post(self, post_id: str) -> Dict[str, Any]:
        """DELETE /posts/{id}."""
        return await self.http_client.delete_post(post_id)

    async def list_channel_posts(
        self,
        channel_id: str,
        page: int = 0,
        per_page: int = 60,
        since: Optional[int] = None,
        before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /channels/{id}/posts."""
        return await self.http_client.list_channel_posts(
            channel_id=channel_id,
            page=page,
            per_page=per_page,
            since=since,
            before=before,
            after=after,
        )

    async def upload_file(
        self,
        channel_id: str,
        file_bytes: bytes,
        filename: str,
    ) -> Dict[str, Any]:
        """POST /files — multipart upload of a single file attachment."""
        return await self.http_client.upload_file(
            channel_id=channel_id,
            file_bytes=file_bytes,
            filename=filename,
        )

    async def get_file_info(self, file_id: str) -> Dict[str, Any]:
        """GET /files/{id}/info."""
        return await self.http_client.get_file_info(file_id)

    async def create_incoming_webhook(
        self,
        channel_id: str,
        display_name: str,
        description: str = "",
        username: Optional[str] = None,
        icon_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /hooks/incoming."""
        return await self.http_client.create_incoming_webhook(
            channel_id=channel_id,
            display_name=display_name,
            description=description,
            username=username,
            icon_url=icon_url,
        )

    async def list_incoming_webhooks(
        self,
        team_id: Optional[str] = None,
        page: int = 0,
        per_page: int = 60,
    ) -> Any:
        """GET /hooks/incoming."""
        return await self.http_client.list_incoming_webhooks(
            team_id=team_id,
            page=page,
            per_page=per_page,
        )

    async def create_outgoing_webhook(
        self,
        team_id: str,
        display_name: str,
        trigger_words: List[str],
        callback_urls: List[str],
        channel_id: Optional[str] = None,
        description: str = "",
        content_type: str = "application/x-www-form-urlencoded",
    ) -> Dict[str, Any]:
        """POST /hooks/outgoing."""
        return await self.http_client.create_outgoing_webhook(
            team_id=team_id,
            display_name=display_name,
            trigger_words=trigger_words,
            callback_urls=callback_urls,
            channel_id=channel_id,
            description=description,
            content_type=content_type,
        )

    async def list_outgoing_webhooks(
        self,
        team_id: Optional[str] = None,
        page: int = 0,
        per_page: int = 60,
    ) -> Any:
        """GET /hooks/outgoing."""
        return await self.http_client.list_outgoing_webhooks(
            team_id=team_id,
            page=page,
            per_page=per_page,
        )

    async def list_bots(
        self,
        page: int = 0,
        per_page: int = 60,
        include_deleted: bool = False,
    ) -> Any:
        """GET /bots."""
        return await self.http_client.list_bots(
            page=page,
            per_page=per_page,
            include_deleted=include_deleted,
        )

    async def list_team_commands(
        self,
        team_id: str,
        custom_only: bool = False,
    ) -> Any:
        """GET /commands?team_id=…"""
        return await self.http_client.list_team_commands(
            team_id=team_id,
            custom_only=custom_only,
        )
