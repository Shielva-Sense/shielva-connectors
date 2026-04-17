"""
Gmail Connector — Main Orchestration Layer
Orchestrates only: delegates all HTTP to client/, all normalization to helpers/.
Zero raw HTTP calls, zero JSON parsing, zero base64 encoding inline.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp
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

from client.http_client import GmailHttpClient
from exceptions import GmailAuthError, GmailMessageNotFoundError, GmailValidationError, GmailAttachmentError
from helpers.normalizer import normalize_message
from helpers.utils import (
    build_raw_email_message,
    calculate_attachment_size,
    epoch_from_datetime,
    validate_email_address,
)

logger = structlog.get_logger(__name__)

# ── Provider-wide constants (bind:true — same for all tenants) ────────────────
AUTH_TYPE = "oauth2_code"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REQUIRED_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
BASE_URL = "https://gmail.googleapis.com/gmail/v1"
API_VERSION = "v1"
MAX_ATTACHMENT_SIZE_MB = 25
DEFAULT_PAGE_SIZE = 20
TOKEN_REFRESH_BUFFER_S = 60  # refresh if token expires within 60 seconds


class GmailConnector(BaseConnector):
    """
    Shielva Gmail Connector.
    Implements bidirectional email operations via Gmail REST API v1.
    """

    CONNECTOR_TYPE = "shielva_gmail"
    CONNECTOR_NAME = "Gmail"
    AUTH_TYPE = AUTH_TYPE
    REQUIRED_SCOPES = REQUIRED_SCOPES

    # OCP — class-level constants; extend without modifying methods
    REQUIRED_CONFIG_KEYS: List[str] = [
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
    # OCP-1 — status-code → (ConnectorHealth, AuthStatus) lookup used by health_check()
    _STATUS_MAP: Dict[int, Any] = {
        401: (ConnectorHealth.DEGRADED, AuthStatus.EXPIRED),
        403: (ConnectorHealth.DEGRADED, AuthStatus.FAILED),
        429: (ConnectorHealth.DEGRADED, AuthStatus.FAILED),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        # User-provided install fields (bind:false — read from self.config)
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.scopes: Any = self.config.get("scopes", "")
        self.auth_url: str = self.config.get("auth_url", "")
        self.token_url: str = self.config.get("token_url", "")
        self.base_url: str = self.config.get("base_url", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", "")
        self.pagination_type: str = self.config.get("pagination_type", "")
        self.api_version: str = self.config.get("api_version", "")

        self._client: Optional[GmailHttpClient] = None
        self.log = logger.bind(
            tenant_id=tenant_id,
            connector_id=connector_id,
            connector_type=self.CONNECTOR_TYPE,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _resolve_token_url(self) -> str:
        return self.config.get("token_url") or TOKEN_URI

    def _resolve_scopes(self) -> List[str]:
        scopes = self.config.get("scopes")
        if isinstance(scopes, list) and scopes:
            return scopes
        if isinstance(scopes, str) and scopes:
            return scopes.split()
        return REQUIRED_SCOPES

    async def _get_client(self) -> GmailHttpClient:
        """
        Build and return a GmailHttpClient with a fresh, valid token.
        Refreshes the token if it is expired or near expiry.
        """
        token = await self.get_token()
        if token is None:
            raise GmailAuthError("No token found — connector not authorized")

        # Refresh if near expiry
        if token.expires_at is not None:
            now = datetime.now(tz=timezone.utc)
            expires = token.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires - now < timedelta(seconds=TOKEN_REFRESH_BUFFER_S):
                token = await self._refresh_token(token)

        from google.oauth2.credentials import Credentials
        credentials = Credentials(
            token=token.access_token,
            refresh_token=token.refresh_token,
            token_uri=self._resolve_token_url(),
            client_id=self.config.get("client_id"),
            client_secret=self.config.get("client_secret"),
            scopes=token.scopes or self._resolve_scopes(),
        )
        self._client = GmailHttpClient(
            credentials=credentials,
            base_url=self.config.get("base_url") or BASE_URL,
        )
        return self._client

    async def _refresh_token(self, token: TokenInfo) -> TokenInfo:
        """Exchange refresh_token for a new access token."""
        if not token.refresh_token:
            raise GmailAuthError("Token expired and no refresh_token available")

        token_uri = self._resolve_token_url()
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": self.config.get("client_id"),
            "client_secret": self.config.get("client_secret"),
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(token_uri, data=payload) as resp:
                    if resp.status != 200:
                        raise GmailAuthError("Token refresh failed")
                    data = await resp.json()
        except aiohttp.ClientError as exc:
            raise GmailAuthError(f"Token refresh network error: {exc}") from exc

        expires_in = data.get("expires_in", 3600)
        new_token = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or token.refresh_token,
            expires_at=datetime.now(tz=timezone.utc) + timedelta(seconds=int(expires_in)),
            token_type=data.get("token_type", "Bearer"),
            scopes=data.get("scope", "").split() if data.get("scope") else token.scopes,
        )
        await self.set_token(new_token)
        self.log.info("gmail.token_refreshed")
        return new_token

    # ── BaseConnector abstract methods ────────────────────────────────────────

    async def install(self, config: Dict[str, Any] = None) -> ConnectorStatus:
        """
        Persist connector config and return PENDING status prompting OAuth.
        No API call is made here.
        """
        if config:
            await self.save_config(config)
            # Refresh instance attributes after saving
            self.client_id = self.config.get("client_id", "")
            self.client_secret = self.config.get("client_secret", "")

        self.log.info("gmail.install", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.PENDING,
            connector_type=self.CONNECTOR_TYPE,
            message="Click Authorize to connect your Gmail account",
        )

    async def authorize(self, auth_data: Dict[str, Any]) -> TokenInfo:
        """
        Exchange OAuth2 authorization code for tokens; persist via set_token().

        Args:
            auth_data: Must contain 'code'. Optionally 'redirect_uri'.

        Returns:
            TokenInfo with access and refresh tokens.
        """
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        if not client_id or not client_secret:
            raise GmailAuthError("client_id and client_secret are required for authorization")

        code = auth_data.get("code")
        if not code:
            raise GmailAuthError("Authorization code is required in auth_data['code']")

        redirect_uri = auth_data.get("redirect_uri") or self.config.get("redirect_uri")
        token_uri = self._resolve_token_url()

        payload = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(token_uri, data=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise GmailAuthError(f"Token exchange failed ({resp.status}): {body}")
                    data = await resp.json()
        except aiohttp.ClientError as exc:
            raise GmailAuthError(f"Token exchange network error: {exc}") from exc

        expires_in = data.get("expires_in", 3600)
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(tz=timezone.utc) + timedelta(seconds=int(expires_in)),
            token_type=data.get("token_type", "Bearer"),
            scopes=data.get("scope", "").split() if data.get("scope") else self._resolve_scopes(),
        )
        await self.set_token(token_info)
        self.log.info("gmail.authorized", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """
        Verify token validity and API reachability via a lightweight profile probe.
        """
        token = await self.get_token()
        if token is None:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message="No token found — connector not authorized",
            )

        try:
            client = await self._get_client()
            await client.get_profile()
            self.log.info("gmail.health_check.healthy")
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message="Gmail API reachable",
            )
        except GmailAuthError as exc:
            self.log.warning("gmail.health_check.auth_error", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.EXPIRED,
                connector_type=self.CONNECTOR_TYPE,
                message=str(exc),
            )
        except Exception as exc:
            self.log.error("gmail.health_check.error", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                connector_type=self.CONNECTOR_TYPE,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """
        Incremental sync of Gmail messages.
        Uses 'after:{epoch}' Gmail search query for time-bounded fetches.
        Paginates via nextPageToken until exhausted.
        """
        self.log.info("gmail.sync.start", full=full, since=str(since))

        query = ""
        if not full and since is not None:
            epoch = epoch_from_datetime(since)
            query = f"after:{epoch}"

        page_token: Optional[str] = None
        page_size = int(self.config.get("page_size") or DEFAULT_PAGE_SIZE)
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            client = await self._get_client()
            while True:
                list_resp = await client.list_messages(
                    query=query,
                    page_token=page_token,
                    max_results=page_size,
                )
                messages = list_resp.get("messages", [])
                documents_found += len(messages)
                next_page_token = list_resp.get("nextPageToken")

                batch: List[NormalizedDocument] = []
                for msg_stub in messages:
                    msg_id = msg_stub["id"]
                    try:
                        raw = await client.get_message(msg_id)
                        doc = normalize_message(raw, self.tenant_id, self.connector_id)
                        batch.append(doc)
                    except GmailMessageNotFoundError:
                        self.log.warning("gmail.sync.message_not_found", message_id=msg_id)
                        documents_failed += 1
                    except Exception as exc:
                        self.log.error("gmail.sync.message_error", message_id=msg_id, error=str(exc))
                        documents_failed += 1

                if batch:
                    await self.ingest_batch(batch, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += len(batch)

                if not next_page_token:
                    break
                page_token = next_page_token

            self.log.info(
                "gmail.sync.complete",
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
            )
            return SyncResult(
                status=SyncStatus.COMPLETED,
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
            )

        except GmailAuthError as exc:
            self.log.error("gmail.sync.auth_error", error=str(exc))
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )
        except Exception as exc:
            self.log.error("gmail.sync.error", error=str(exc))
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── User-requested standalone operations ─────────────────────────────────

    async def list_emails(
        self,
        page_token: Optional[str] = None,
        max_results: int = DEFAULT_PAGE_SIZE,
        query: Optional[str] = None,
    ) -> List[NormalizedDocument]:
        """
        Return a single page of normalized email documents.
        nextPageToken (if present) is stored in each document's metadata.
        """
        client = await self._get_client()
        list_resp = await client.list_messages(
            query=query or "",
            page_token=page_token,
            max_results=max_results,
        )
        messages = list_resp.get("messages", [])
        next_page_token = list_resp.get("nextPageToken")

        docs: List[NormalizedDocument] = []
        for msg_stub in messages:
            raw = await client.get_message(msg_stub["id"])
            doc = normalize_message(
                raw,
                self.tenant_id,
                self.connector_id,
                next_page_token=next_page_token,
            )
            docs.append(doc)

        self.log.info("gmail.list_emails", count=len(docs))
        return docs

    async def list_email(self, message_id: str) -> NormalizedDocument:
        """
        Fetch and normalize a single email by Gmail message ID.
        Raises GmailMessageNotFoundError if the message does not exist.
        """
        client = await self._get_client()
        raw = await client.get_message(message_id)
        doc = normalize_message(raw, self.tenant_id, self.connector_id)
        self.log.info("gmail.list_email", message_id=message_id)
        return doc

    async def search_email(
        self,
        query: str,
        page_token: Optional[str] = None,
        max_results: int = DEFAULT_PAGE_SIZE,
    ) -> List[NormalizedDocument]:
        """
        Search Gmail messages with a query string (Gmail search syntax).
        Returns a single page of normalized documents.
        """
        client = await self._get_client()
        list_resp = await client.list_messages(
            query=query,
            page_token=page_token,
            max_results=max_results,
        )
        messages = list_resp.get("messages", [])
        next_page_token = list_resp.get("nextPageToken")

        docs: List[NormalizedDocument] = []
        for msg_stub in messages:
            raw = await client.get_message(msg_stub["id"])
            doc = normalize_message(
                raw,
                self.tenant_id,
                self.connector_id,
                next_page_token=next_page_token,
            )
            docs.append(doc)

        self.log.info("gmail.search_email", query=query, count=len(docs))
        return docs

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        reply_to: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Send an email from the authenticated Gmail account.
        Validates recipient address and attachment size before sending.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text email body.
            cc: Optional CC addresses.
            bcc: Optional BCC addresses.
            reply_to: Optional Reply-To address.
            attachments: Optional list of dicts with 'filename', 'data' (bytes), 'mimetype'.

        Returns:
            Dict with Gmail message 'id', 'threadId', 'labelIds'.
        """
        validate_email_address(to)
        calculate_attachment_size(attachments)
        raw_message = build_raw_email_message(to, subject, body, cc, bcc, reply_to, attachments)

        client = await self._get_client()
        result = await client.send_message(raw_message)
        self.log.info("gmail.send_email", to=to, message_id=result.get("id"))
        return result

    async def delete_email(self, message_id: str, permanent: bool = False) -> None:
        """
        Delete an email by moving it to Trash or permanently deleting it.

        Args:
            message_id: Gmail message ID.
            permanent: If False (default), move to Trash. If True, permanently delete.

        Raises:
            GmailMessageNotFoundError: If the message does not exist.
        """
        client = await self._get_client()
        if permanent:
            await client.delete_message_permanent(message_id)
            self.log.info("gmail.delete_email.permanent", message_id=message_id)
        else:
            await client.trash_message(message_id)
            self.log.info("gmail.delete_email.trash", message_id=message_id)
