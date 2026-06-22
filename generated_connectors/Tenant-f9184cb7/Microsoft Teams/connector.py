"""Microsoft Teams connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from shared.base_connector import BaseConnector

from client.http_client import MicrosoftTeamsHTTPClient
from exceptions import (
    MicrosoftTeamsAuthError,
    MicrosoftTeamsError,
    MicrosoftTeamsNetworkError,
)
from helpers.utils import normalize_message, with_retry
from models import (
    AuthStatus,
    ConnectorHealth,
    ConnectorDocument,
    InstallResult,
    HealthCheckResult,
    SyncResult,
    SyncStatus,
)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_OAUTH_AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_OAUTH_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

_DEFAULT_SCOPES = [
    "https://graph.microsoft.com/Team.ReadBasic.All",
    "https://graph.microsoft.com/Channel.ReadBasic.All",
    "https://graph.microsoft.com/ChannelMessage.Read.All",
    "offline_access",
]


class MicrosoftTeamsConnector(BaseConnector):
    """Shielva connector for Microsoft Teams via the Microsoft Graph API v1.0."""

    CONNECTOR_TYPE = "microsoft_teams"
    CONNECTOR_NAME = "Microsoft Teams"
    AUTH_TYPE = "oauth2"

    REQUIRED_SCOPES: List[str] = _DEFAULT_SCOPES

    REQUIRED_CONFIG_KEYS: List[str] = ["client_id", "client_secret"]

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        super().__init__(tenant_id, connector_id, cfg)
        self._http_client: Optional[MicrosoftTeamsHTTPClient] = None

    def _ensure_client(self) -> MicrosoftTeamsHTTPClient:
        if self._http_client is None:
            self._http_client = MicrosoftTeamsHTTPClient(base_url=_GRAPH_BASE)
        return self._http_client

    def _get_access_token(self) -> str:
        return self.config.get("access_token", "")

    def _get_client_id(self) -> str:
        return self.config.get("client_id", "")

    def _get_client_secret(self) -> str:
        return self.config.get("client_secret", "")

    def _get_redirect_uri(self) -> str:
        return self.config.get("redirect_uri", "")

    def _get_tenant_hint(self) -> str:
        return self.config.get("tenant_hint", "common")

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        """Validate that client_id and client_secret are present."""
        client_id = self._get_client_id()
        client_secret = self._get_client_secret()

        if not client_id or not client_secret:
            missing = []
            if not client_id:
                missing.append("client_id")
            if not client_secret:
                missing.append("client_secret")
            msg = f"Missing required fields: {', '.join(missing)}"
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message=msg,
            )

        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Connector installed — OAuth2 credentials present. Complete authorization to connect.",
        )

    # ── authorize ─────────────────────────────────────────────────────────────

    def authorize(self, state: str = "") -> str:
        """Return the Microsoft OAuth2 authorization URL.

        The caller should redirect the user to this URL to begin the OAuth flow.
        """
        client_id = self._get_client_id()
        redirect_uri = self._get_redirect_uri()
        tenant_hint = self._get_tenant_hint() or "common"

        # Build the authorization URL using the tenant-specific endpoint when provided
        authorize_url = (
            f"https://login.microsoftonline.com/{tenant_hint}/oauth2/v2.0/authorize"
        )

        params: Dict[str, str] = {
            "client_id": client_id,
            "response_type": "code",
            "scope": " ".join(_DEFAULT_SCOPES),
            "response_mode": "query",
        }
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        if state:
            params["state"] = state

        return f"{authorize_url}?{urlencode(params)}"

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        """Call GET /me to verify the access token is valid."""
        access_token = self._get_access_token()
        try:
            data = await with_retry(
                lambda: self._ensure_client().get_me(access_token),
                max_attempts=2,
            )
            display_name = data.get("displayName", data.get("userPrincipalName", "unknown user"))
            msg = f"Connected as: {display_name}"

            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
            )
        except MicrosoftTeamsAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: Optional[Any] = None,
        kb_id: str = "",
    ) -> Any:
        """Sync messages from all joined teams and their channels.

        Fetches all teams the user has joined, then all channels in each team,
        then all messages in each channel.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        all_documents: List[ConnectorDocument] = []

        try:
            teams = await self.list_teams()

            for team in teams:
                team_id = team.get("id", "")
                team_name = team.get("displayName", team_id)
                if not team_id:
                    continue

                try:
                    channels = await self.list_channels(team_id)
                except MicrosoftTeamsAuthError:
                    raise
                except Exception as exc:
                    documents_failed += 1
                    continue

                for channel in channels:
                    channel_id = channel.get("id", "")
                    if not channel_id:
                        continue

                    try:
                        messages = await self.list_messages(team_id, channel_id)
                        documents_found += len(messages)

                        for message in messages:
                            try:
                                doc = normalize_message(message, team_id, channel_id)
                                all_documents.append(doc)
                                documents_synced += 1
                            except Exception:
                                documents_failed += 1

                    except MicrosoftTeamsAuthError:
                        raise
                    except Exception:
                        documents_failed += 1

            status = SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            msg = (
                f"Synced {documents_synced}/{documents_found} messages "
                f"from {len(teams)} teams"
            )

            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=msg,
                documents=all_documents,
            )

        except Exception as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
                documents=all_documents,
            )

    # ── convenience methods ───────────────────────────────────────────────────

    async def list_teams(self) -> List[Dict[str, Any]]:
        """GET /me/joinedTeams — list all teams the user has joined."""
        access_token = self._get_access_token()
        return await with_retry(
            lambda: self._ensure_client().get_joined_teams(access_token),
            max_attempts=3,
        )

    async def list_channels(self, team_id: str) -> List[Dict[str, Any]]:
        """GET /teams/{team_id}/channels — list channels in a team."""
        access_token = self._get_access_token()
        return await with_retry(
            lambda: self._ensure_client().get_channels(access_token, team_id),
            max_attempts=3,
        )

    async def list_messages(
        self,
        team_id: str,
        channel_id: str,
    ) -> List[Dict[str, Any]]:
        """GET /teams/{team_id}/channels/{channel_id}/messages — list messages.

        Handles @odata.nextLink pagination automatically.
        """
        access_token = self._get_access_token()
        return await with_retry(
            lambda: self._ensure_client().get_messages(access_token, team_id, channel_id),
            max_attempts=3,
        )

    async def list_channel_messages(
        self,
        team_id: str,
        channel_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """GET /teams/{team_id}/channels/{channel_id}/messages?$top={limit}.

        Canonical capability method. Delegates to get_channel_messages which
        supports the `top` query parameter for page size control.
        Handles @odata.nextLink pagination automatically.
        """
        access_token = self._get_access_token()
        return await with_retry(
            lambda: self._ensure_client().get_channel_messages(
                access_token, team_id, channel_id, top=limit
            ),
            max_attempts=3,
        )

    async def get_message(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
    ) -> Dict[str, Any]:
        """GET /teams/{team_id}/channels/{channel_id}/messages/{message_id}."""
        access_token = self._get_access_token()
        return await with_retry(
            lambda: self._ensure_client().get_message(
                access_token, team_id, channel_id, message_id
            ),
            max_attempts=3,
        )

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> "MicrosoftTeamsConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()


