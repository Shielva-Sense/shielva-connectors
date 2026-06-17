"""Gmail connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
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

from client.http_client import GmailHTTPClient
from exceptions import GmailAPIError, GmailAuthError, GmailConnectorError
from helpers.normalizer import normalize_message
from helpers.utils import base64url_encode, build_mime_message, with_retry

logger = structlog.get_logger(__name__)

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"


class GmailConnector(BaseConnector):
    """Shielva connector for the Google Gmail API."""

    CONNECTOR_TYPE = "google_gmail_connector"
    CONNECTOR_NAME = "Google Gmail"
    AUTH_TYPE = "oauth2_code"
    AUTH_URI = AUTH_URI
    TOKEN_URI = TOKEN_URI

    REQUIRED_SCOPES: List[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
    ]

    REQUIRED_CONFIG_KEYS = [
        "client_id",
        "client_secret",
        "scopes",
        "auth_url",
        "token_url",
        "base_url",
        "rate_limit_per_min",
        "pagination_type",
        "api_version",
    ]

    _STATUS_MAP = {
        401: (ConnectorHealth.DEGRADED, AuthStatus.TOKEN_EXPIRED),
        403: (ConnectorHealth.DEGRADED, AuthStatus.AUTHENTICATED),
        429: (ConnectorHealth.DEGRADED, AuthStatus.AUTHENTICATED),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.scopes: str = self.config.get("scopes", "")
        self.auth_url: str = self.config.get("auth_url", "")
        self.token_url: str = self.config.get("token_url", "")
        self.base_url: str = self.config.get("base_url", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", "")
        self.pagination_type: str = self.config.get("pagination_type", "")
        self.api_version: str = self.config.get("api_version", "")

        base = self.base_url or _GMAIL_BASE
        self.http_client = GmailHTTPClient(base_url=base)

    # ── Internal helpers ────────────────────────────────────────────────────

    async def _get_valid_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        token_info = await self.ensure_token()
        return token_info.access_token

    async def on_token_refresh(self) -> TokenInfo:
        """Refresh the OAuth2 access token using the stored refresh token."""
        if not self._token_info or not self._token_info.refresh_token:
            raise RefreshError("No refresh token available")

        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")
        token_uri = self.config.get("token_url") or TOKEN_URI

        stored_token = self._token_info.refresh_token
        data = await self.http_client.post_form_data(
            url=token_uri,
            payload={
                "grant_type": "refresh_token",
                "refresh_token": stored_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            context="on_token_refresh",
        )

        expires_in = int(data.get("expires_in", 3600))
        new_scopes = data.get("scope", "").split() if data.get("scope") else list(self._token_info.scopes)
        return TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or stored_token,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=new_scopes,
        )

    # ── Abstract method implementations ────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and return connector status."""
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        if not client_id or not client_secret:
            logger.warning(
                "gmail.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )

        await self.save_config({"client_id": client_id, "client_secret": client_secret})
        logger.info("gmail.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Connector installed — complete OAuth to connect",
        )

    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Exchange OAuth authorization code for access + refresh tokens."""
        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")
        token_uri = self.config.get("token_url") or TOKEN_URI
        # redirect_uri MUST come from self.config — the gateway sets it at deploy/check
        # time. `state` carries the connector_id, not a URL, so reading it from state
        # produces an empty redirect_uri and Google rejects with 400 invalid_request.
        redirect_uri = self.config.get("redirect_uri", "")

        data = await self.http_client.post_form_data(
            url=token_uri,
            payload={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            context="authorize",
        )

        expires_in = int(data.get("expires_in", 3600))
        scopes = data.get("scope", "").split() if data.get("scope") else list(self.REQUIRED_SCOPES)
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )
        await self.set_token(token_info)
        logger.info("gmail.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Check Gmail API connectivity by fetching the user profile."""
        try:
            access_token = await self._get_valid_token()
            await with_retry(
                lambda: self.http_client.get_profile(access_token),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Gmail API reachable",
            )
        except GmailAuthError:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message="Token expired — re-authorize the connector",
            )
        except RefreshError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.EXPIRED,
                message=str(exc),
            )
        except GmailConnectorError as exc:
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
        """Sync Gmail messages into the Shielva knowledge base.

        Performs incremental sync via the history API when a checkpoint exists,
        falling back to a full page-token scan when *full* is True or no checkpoint.
        """
        access_token = await self._get_valid_token()
        last_history_id: Optional[str] = await self.get_metadata("last_history_id")

        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        latest_history_id: Optional[str] = None

        try:
            if not full and last_history_id:
                message_ids = await self._collect_history_ids(access_token, last_history_id)
            else:
                message_ids, latest_history_id = await self._collect_all_message_ids(access_token)

            documents_found = len(message_ids)

            for msg_id in message_ids:
                try:
                    raw = await with_retry(
                        lambda mid=msg_id: self.http_client.get_message(access_token, mid),
                        max_retries=3,
                    )
                    doc = normalize_message(raw, self.connector_id, self.tenant_id)
                    if raw.get("historyId") and (
                        latest_history_id is None
                        or int(raw["historyId"]) > int(latest_history_id)
                    ):
                        latest_history_id = raw["historyId"]
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "gmail.sync.message_failed",
                        message_id=msg_id,
                        error=str(exc),
                    )
                    documents_failed += 1

            if latest_history_id:
                await self.set_metadata("last_history_id", latest_history_id)

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} messages",
            )

        except Exception as exc:
            logger.error("gmail.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    async def _collect_all_message_ids(
        self, access_token: str
    ) -> tuple[List[str], Optional[str]]:
        """Page through /users/me/messages and return all IDs + last history_id."""
        ids: List[str] = []
        page_token: Optional[str] = None
        latest_history_id: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda pt=page_token: self.http_client.list_messages(
                    access_token, query="in:inbox", max_results=500, page_token=pt
                ),
                max_retries=3,
            )
            for stub in resp.get("messages", []):
                ids.append(stub["id"])
            if resp.get("historyId") and (
                latest_history_id is None
                or int(resp["historyId"]) > int(latest_history_id)
            ):
                latest_history_id = resp.get("historyId")
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return ids, latest_history_id

    async def _collect_history_ids(
        self, access_token: str, start_history_id: str
    ) -> List[str]:
        """Return message IDs added since *start_history_id* via the history API."""
        ids: List[str] = []
        page_token: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda pt=page_token: self.http_client.list_history(
                    access_token,
                    start_history_id=start_history_id,
                    history_types=["messageAdded"],
                    max_results=500,
                    page_token=pt,
                ),
                max_retries=3,
            )
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_id = added.get("message", {}).get("id")
                    if msg_id:
                        ids.append(msg_id)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return ids

    # ── User-requested standalone methods ───────────────────────────────────

    async def list_emails(
        self,
        query: str = "",
        max_results: int = 500,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List Gmail message stubs matching *query*.

        Returns the raw API response dict: {messages, nextPageToken, resultSizeEstimate}.
        """
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_messages(
                access_token,
                query=query,
                max_results=max_results,
                page_token=page_token,
            ),
            max_retries=3,
        )

    async def get_email(self, message_id: str) -> NormalizedDocument:
        """Fetch a full Gmail message and return it as a NormalizedDocument."""
        access_token = await self._get_valid_token()
        raw = await with_retry(
            lambda: self.http_client.get_message(access_token, message_id),
            max_retries=3,
        )
        return normalize_message(raw, self.connector_id, self.tenant_id)

    async def modify_message(
        self,
        message_id: str,
        add_labels: Optional[List[str]] = None,
        remove_labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Add or remove labels on a Gmail message.

        Returns the raw API response dict.
        """
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.execute_modify_message(
                access_token,
                message_id=message_id,
                add_label_ids=add_labels,
                remove_label_ids=remove_labels,
            ),
            max_retries=3,
        )

    async def read_email(self, message_id: str) -> Dict[str, Any]:
        """Return the raw Gmail API message object for *message_id*.

        Delegates directly to GmailHTTPClient.get_message() — no extra HTTP path.
        """
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get_message(access_token, message_id),
            max_retries=3,
        )

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send an email via the Gmail API.

        Builds a MIME message, base64url-encodes it (no padding), and delegates
        to GmailHTTPClient.execute_send_message().

        `body`      — plain-text part (always sent as the fallback).
        `html_body` — optional HTML part. When supplied, the message is sent as
                      `multipart/alternative` so clients that render HTML do so and
                      others fall back to plain text. Existing callers that omit
                      `html_body` get the original plain-only behavior.

        Requires the gmail.send scope — raises PermissionError if missing.
        Returns the raw API response: {id, threadId, labelIds}.
        """
        mime_msg = build_mime_message(
            to=to, subject=subject, body=body, cc=cc, bcc=bcc, html_body=html_body,
        )
        raw = base64url_encode(mime_msg.as_bytes())
        access_token = await self._get_valid_token()
        return await self.http_client.execute_send_message(access_token, raw)

    async def add_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Gmail draft (does not send).

        Uses the same MIME-building path as send_email() for consistency, including
        optional HTML support via `html_body` (multipart/alternative).
        Returns the raw API draft response: {id, message: {id, threadId, labelIds}}.
        """
        mime_msg = build_mime_message(
            to=to, subject=subject, body=body, cc=cc, bcc=bcc, html_body=html_body,
        )
        raw = base64url_encode(mime_msg.as_bytes())
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.execute_create_draft(access_token, raw),
            max_retries=3,
        )

    async def post_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Public alias for send_email() — provided for API surface completeness."""
        return await self.send_email(
            to=to, subject=subject, body=body, cc=cc, bcc=bcc, html_body=html_body,
        )
