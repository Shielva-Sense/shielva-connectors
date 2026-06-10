"""
Gmail Connector — hand-written BaseConnector subclass for the Shielva connector platform.

Capabilities:
  • READ  — sync() lists recent messages, fetches each, normalizes, and ingests them.
  • SEND  — send_email() posts a base64url RFC822 message to messages.send.

Auth: OAuth2 Authorization Code grant (oauth2_code).
  - The base class builds the consent URL via get_oauth_url() (reads AUTH_URI / client_id / REQUIRED_SCOPES).
  - authorize() exchanges the code for tokens and persists them via set_token() (Redis).
  - ensure_token() (used before every API call) auto-refreshes using the refresh_token.

SECURITY: client_id / client_secret are NEVER hardcoded here. They are read from
self.config, which the gateway populates from the AES-256-GCM-encrypted credential
store (Redis, keyed by MASTER_KEY) at check/deploy time. Tokens live only in Redis.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

import structlog

from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import GMAIL_API_BASE, GOOGLE_TOKEN_URI, GmailClient
from helpers.gmail_utils import build_raw_email_message
from helpers.normalizer import normalize_message

logger = structlog.get_logger(__name__)

# ── Module-level OAuth constants (base_connector resolves these via sys.modules too) ──
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GmailConnector(BaseConnector):
    """Read + send Gmail on behalf of an authenticated Google user."""

    # ── Connector identity ──────────────────────────────────────────────────
    CONNECTOR_TYPE = "gmail"
    CONNECTOR_NAME = "Gmail"
    AUTH_TYPE = "oauth2_code"

    # ── OAuth2 class constants (consumed by base get_oauth_url / probe) ──────
    AUTH_URI = AUTH_URI
    TOKEN_URI = TOKEN_URI
    REQUIRED_SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]

    GMAIL_API_BASE = GMAIL_API_BASE

    def __init__(self, tenant_id: str, connector_id: str, config: dict | None = None) -> None:
        super().__init__(tenant_id, connector_id, config)
        # Credentials are read from config — never hardcoded.
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self._client = GmailClient(api_base=self.GMAIL_API_BASE, token_uri=GOOGLE_TOKEN_URI)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def install(self, config: dict | None = None) -> ConnectorStatus:
        """Set up the connector. Returns PENDING — user must complete OAuth next."""
        if config:
            # /connectors/check passes config explicitly for some auth types.
            await self.save_config(config)
            self.client_id = self.config.get("client_id", self.client_id)
            self.client_secret = self.config.get("client_secret", self.client_secret)

        if not self.client_id or not self.client_secret:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                error="client_id and client_secret are required.",
            )

        self._status.auth_status = AuthStatus.PENDING
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.PENDING,
            connector_type=self.CONNECTOR_TYPE,
            message="Click Authorize to connect your Gmail account.",
        )

    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Exchange the OAuth authorization code for access + refresh tokens."""
        redirect_uri = self.config.get("redirect_uri")
        if not redirect_uri:
            raise ValueError("redirect_uri missing from connector config — gateway should inject it.")

        data = await self._client.exchange_code(
            code=auth_code,
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=redirect_uri,
        )

        token_info = self._token_info_from_response(data)
        await self.set_token(token_info)
        logger.info("gmail.authorized", connector_id=self.connector_id,
                    has_refresh=bool(token_info.refresh_token))
        return token_info

    async def on_token_refresh(self) -> TokenInfo:
        """Refresh the access token using the stored refresh_token (called by ensure_token)."""
        if not self._token_info or not self._token_info.refresh_token:
            raise RuntimeError("No refresh_token available to refresh Gmail access token.")

        data = await self._client.refresh_token(
            refresh_token=self._token_info.refresh_token,
            client_id=self.client_id,
            client_secret=self.client_secret,
        )
        # Google omits refresh_token on refresh responses — carry the old one forward.
        if not data.get("refresh_token"):
            data["refresh_token"] = self._token_info.refresh_token
        return self._token_info_from_response(data)

    def _token_info_from_response(self, data: dict) -> TokenInfo:
        expires_in = int(data.get("expires_in", 3600))
        scope_str = data.get("scope", "")
        return TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=datetime.utcnow() + timedelta(seconds=expires_in),
            scopes=scope_str.split() if scope_str else list(self.REQUIRED_SCOPES),
            raw=data,
        )

    # ── Health ──────────────────────────────────────────────────────────────

    async def health_check(self) -> ConnectorStatus:
        """Live API probe: getProfile. Auto-refreshes the token first."""
        # No token at all → OAuth hasn't been completed yet. Report PENDING so the
        # deploy/check flow generates the consent URL (vs TOKEN_EXPIRED, which means
        # a token existed but could not be refreshed).
        if self._token_info is None:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.PENDING,
                connector_type=self.CONNECTOR_TYPE,
                message="Click Authorize to connect your Gmail account.",
            )
        try:
            token = await self.ensure_token()
        except Exception as e:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                connector_type=self.CONNECTOR_TYPE,
                error=f"Token unavailable or refresh failed: {e}",
            )

        try:
            profile = await self._client.get_profile(access_token=token.access_token)
            self._status.health = ConnectorHealth.HEALTHY
            self._status.auth_status = AuthStatus.CONNECTED
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message=f"Connected as {profile.get('emailAddress', 'unknown')} "
                        f"({profile.get('messagesTotal', 0)} messages).",
                metadata={"email": profile.get("emailAddress", "")},
            )
        except Exception as e:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.FAILED,
                connector_type=self.CONNECTOR_TYPE,
                error=f"Gmail API call failed: {e}",
            )

    # ── READ: sync ──────────────────────────────────────────────────────────

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """List recent messages, fetch + normalize each, and ingest the batch."""
        result = SyncResult(
            status=SyncStatus.SYNCING,
            connector_id=self.connector_id,
            started_at=datetime.utcnow(),
        )

        try:
            token = await self.ensure_token()
        except Exception as e:
            result.status = SyncStatus.FAILED
            result.errors.append(f"Auth failed: {e}")
            result.completed_at = datetime.utcnow()
            return result

        max_results = int(self.config.get("max_results", 10))
        query = self.config.get("sync_query") or None

        try:
            refs = await self._client.list_messages(
                access_token=token.access_token, max_results=max_results, query=query
            )
            result.documents_found = len(refs)

            docs: List[NormalizedDocument] = []
            for ref in refs:
                mid = ref.get("id")
                if not mid:
                    continue
                try:
                    raw = await self._client.get_message(access_token=token.access_token, message_id=mid)
                    docs.append(
                        normalize_message(
                            raw, tenant_id=self.tenant_id, connector_id=self.connector_id
                        )
                    )
                except Exception as e:
                    result.documents_failed += 1
                    result.errors.append(f"message {mid}: {e}")

            if docs:
                ok = await self.ingest_batch(docs, kb_id=kb_id or "", webhook_url=webhook_url)
                result.documents_synced = len(docs) if ok else 0
                if not ok:
                    result.errors.append("Ingestion service rejected the batch.")

            result.status = (
                SyncStatus.COMPLETED if not result.errors else SyncStatus.PARTIAL
            )
        except Exception as e:
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))

        result.completed_at = datetime.utcnow()
        logger.info(
            "gmail.sync_complete",
            connector_id=self.connector_id,
            found=result.documents_found,
            synced=result.documents_synced,
            failed=result.documents_failed,
        )
        return result

    # ── SEND ────────────────────────────────────────────────────────────────

    async def send_email(self, to: str, subject: str, body: str) -> dict:
        """Send an email via the Gmail API. Returns {id, threadId, labelIds}.

        Requires the gmail.send scope. Builds a base64url RFC822 message and
        posts it to users/me/messages/send.
        """
        token = await self.ensure_token()
        sender: Optional[str] = (self._status.metadata or {}).get("email")
        raw_b64url = build_raw_email_message(to=to, subject=subject, body=body, sender=sender)
        sent = await self._client.send_message(
            access_token=token.access_token, raw_b64url=raw_b64url
        )
        logger.info("gmail.email_sent", connector_id=self.connector_id, message_id=sent.get("id"))
        return sent
