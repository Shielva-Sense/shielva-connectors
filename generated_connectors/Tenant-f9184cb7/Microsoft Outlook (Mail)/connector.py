"""Microsoft Outlook (Mail) connector — orchestration only.

All HTTP calls   → client/http_client.py
All normalization → helpers/normalizer.py
All utilities    → helpers/utils.py
"""
from __future__ import annotations

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

from client.http_client import OutlookMailHTTPClient
from exceptions import (
    OutlookMailAuthError,
    OutlookMailError,
    OutlookMailNetworkError,
    OutlookMailNotFound,
)
from helpers.normalizer import normalize_message
from helpers.utils import build_send_mail_payload, build_message_payload, with_retry

logger = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_AUTH_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_DEFAULT_SCOPES = "Mail.Read Mail.Send Mail.ReadWrite offline_access"


class OutlookMailConnector(BaseConnector):
    """Shielva connector for Microsoft Outlook mail via Microsoft Graph."""

    CONNECTOR_TYPE = "outlook_mail"
    CONNECTOR_NAME = "Microsoft Outlook (Mail)"
    AUTH_TYPE = "oauth2_code"

    REQUIRED_SCOPES: List[str] = [
        "Mail.Read",
        "Mail.Send",
        "Mail.ReadWrite",
        "offline_access",
    ]

    # Public — only the truly required fields. Azure tenant / scopes / urls
    # all default sensibly for the multi-tenant ("common") install.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "client_id",
        "client_secret",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "TOKEN_EXPIRED"),
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
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        # Azure tenant ("common" / "organizations" / GUID) — distinct from
        # Shielva's tenant_id (kept on self.tenant_id by the base class).
        self.azure_tenant: str = self.config.get("tenant_id") or "common"
        self.scopes: str = self.config.get("scopes") or _DEFAULT_SCOPES
        self.auth_url: str = (
            self.config.get("auth_url")
            or _AUTH_URL_TEMPLATE.format(tenant=self.azure_tenant)
        )
        self.token_url: str = (
            self.config.get("token_url")
            or _TOKEN_URL_TEMPLATE.format(tenant=self.azure_tenant)
        )
        self.base_url: str = self.config.get("base_url") or _GRAPH_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 120)

        self.http_client = OutlookMailHTTPClient(base_url=self.base_url)

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _get_valid_token(self) -> str:
        """Return a valid access token, refreshing via BaseConnector if needed."""
        token_info = await self.ensure_token()
        return token_info.access_token

    async def _refresh_access_token(self) -> str:
        """Token refresher passed into the HTTP client for in-flight 401 recovery."""
        token_info = await self.on_token_refresh()
        await self.set_token(token_info)
        return token_info.access_token

    async def on_token_refresh(self) -> TokenInfo:
        """Refresh the access token using the stored refresh_token."""
        if not self._token_info or not self._token_info.refresh_token:
            raise RefreshError("No refresh token available")

        stored_refresh = self._token_info.refresh_token
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": stored_refresh,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scopes,
        }
        data = await self.http_client.post_form_data(
            url=self.token_url, payload=payload, context="on_token_refresh",
        )

        expires_in = int(data.get("expires_in", 3600))
        new_scopes = (
            data.get("scope", "").split()
            if data.get("scope")
            else list(self._token_info.scopes)
        )
        return TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or stored_refresh,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=new_scopes,
        )

    # ── Abstract method implementations ────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time credentials and persist the config."""
        if not self.client_id or not self.client_secret:
            logger.warning(
                "outlook_mail.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )

        await self.save_config({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "tenant_id": self.azure_tenant,
        })
        logger.info("outlook_mail.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Connector installed — complete OAuth to connect",
        )

    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Exchange an OAuth authorization code for access + refresh tokens."""
        redirect_uri = self.config.get("redirect_uri", "")
        payload = {
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
            "scope": self.scopes,
        }
        data = await self.http_client.post_form_data(
            url=self.token_url, payload=payload, context="authorize",
        )
        expires_in = int(data.get("expires_in", 3600))
        scopes = (
            data.get("scope", "").split()
            if data.get("scope")
            else list(self.REQUIRED_SCOPES)
        )
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )
        await self.set_token(token_info)
        logger.info("outlook_mail.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Confirm Graph reachability by fetching /me."""
        try:
            await with_retry(
                lambda: self.http_client.get_me(
                    token_provider=self._get_valid_token,
                    token_refresher=self._refresh_access_token,
                ),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Microsoft Graph reachable",
            )
        except OutlookMailAuthError:
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
        except OutlookMailError as exc:
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
        """Sync inbox messages into the Shielva knowledge base.

        Uses a ``receivedDateTime`` watermark stored in metadata as the
        incremental cursor; ``full=True`` resets the cursor and re-syncs.
        """
        last_received: Optional[str] = None
        if not full:
            last_received = await self.get_metadata("last_received_at")

        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        newest_seen: Optional[str] = last_received

        try:
            filter_expr = (
                f"receivedDateTime gt {last_received}" if last_received else None
            )
            page = await with_retry(
                lambda: self.http_client.list_messages(
                    token_provider=self._get_valid_token,
                    token_refresher=self._refresh_access_token,
                    folder="inbox",
                    top=50,
                    filter=filter_expr,
                ),
                max_retries=3,
            )
            stubs = page.get("value", []) or []
            documents_found = len(stubs)

            for stub in stubs:
                msg_id = stub.get("id")
                if not msg_id:
                    continue
                try:
                    raw = await with_retry(
                        lambda mid=msg_id: self.http_client.get_message(
                            mid,
                            token_provider=self._get_valid_token,
                            token_refresher=self._refresh_access_token,
                        ),
                        max_retries=3,
                    )
                    doc = normalize_message(raw, self.connector_id, self.tenant_id)
                    received = raw.get("receivedDateTime")
                    if received and (newest_seen is None or received > newest_seen):
                        newest_seen = received
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:  # noqa: BLE001 — per-doc isolation
                    logger.error(
                        "outlook_mail.sync.message_failed",
                        message_id=msg_id, error=str(exc),
                    )
                    documents_failed += 1

            if newest_seen and newest_seen != last_received:
                await self.set_metadata("last_received_at", newest_seen)

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} messages",
            )

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "outlook_mail.sync.failed", error=str(exc), connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── User-facing standalone APIs ───────────────────────────────────────

    async def list_messages(
        self,
        folder: str = "inbox",
        top: int = 25,
        skip: int = 0,
        filter: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List messages in a folder. Returns the raw Graph response."""
        return await with_retry(
            lambda: self.http_client.list_messages(
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
                folder=folder, top=top, skip=skip, filter=filter, search=search,
            ),
            max_retries=3,
        )

    async def get_message(self, message_id: str) -> Dict[str, Any]:
        """Fetch a single message by ID."""
        return await with_retry(
            lambda: self.http_client.get_message(
                message_id,
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
            ),
            max_retries=3,
        )

    async def send_mail(
        self,
        to: List[str],
        subject: str,
        body: str,
        body_type: str = "HTML",
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Send an email via /me/sendMail. Returns {} on 202 Accepted."""
        payload = build_send_mail_payload(
            to=to, subject=subject, body=body, body_type=body_type,
            cc=cc, bcc=bcc, attachments=attachments,
        )
        return await self.http_client.send_mail(
            token_provider=self._get_valid_token,
            token_refresher=self._refresh_access_token,
            message_payload=payload,
        )

    async def create_draft(
        self,
        to: List[str],
        subject: str,
        body: str,
    ) -> Dict[str, Any]:
        """Create a draft message via POST /me/messages."""
        payload = build_message_payload(
            to=to, subject=subject, body=body, body_type="HTML",
        )
        return await with_retry(
            lambda: self.http_client.create_draft(
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
                message_payload=payload,
            ),
            max_retries=3,
        )

    async def reply_message(self, message_id: str, comment: str) -> Dict[str, Any]:
        """Reply to a message via POST /me/messages/{id}/reply."""
        return await with_retry(
            lambda: self.http_client.reply_message(
                message_id, comment,
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
            ),
            max_retries=3,
        )

    async def forward_message(
        self,
        message_id: str,
        to: List[str],
        comment: str = "",
    ) -> Dict[str, Any]:
        """Forward a message via POST /me/messages/{id}/forward."""
        from helpers.utils import to_recipients
        return await with_retry(
            lambda: self.http_client.forward_message(
                message_id, to_recipients(to), comment,
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
            ),
            max_retries=3,
        )

    async def move_message(
        self,
        message_id: str,
        destination_folder_id: str,
    ) -> Dict[str, Any]:
        """Move a message via POST /me/messages/{id}/move."""
        return await with_retry(
            lambda: self.http_client.move_message(
                message_id, destination_folder_id,
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
            ),
            max_retries=3,
        )

    async def delete_message(self, message_id: str) -> Dict[str, Any]:
        """Delete a message via DELETE /me/messages/{id}."""
        return await self.http_client.delete_message(
            message_id,
            token_provider=self._get_valid_token,
            token_refresher=self._refresh_access_token,
        )

    async def list_mail_folders(self) -> Dict[str, Any]:
        """List the signed-in user's mail folders."""
        return await with_retry(
            lambda: self.http_client.list_mail_folders(
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
            ),
            max_retries=3,
        )

    async def create_mail_folder(self, display_name: str) -> Dict[str, Any]:
        """Create a new mail folder."""
        return await self.http_client.create_mail_folder(
            display_name,
            token_provider=self._get_valid_token,
            token_refresher=self._refresh_access_token,
        )

    async def mark_as_read(
        self,
        message_id: str,
        is_read: bool = True,
    ) -> Dict[str, Any]:
        """Mark a message as read/unread via PATCH /me/messages/{id}."""
        return await self.http_client.patch_message(
            message_id, {"isRead": bool(is_read)},
            token_provider=self._get_valid_token,
            token_refresher=self._refresh_access_token,
        )

    async def search_messages(
        self,
        query: str,
        top: int = 25,
    ) -> Dict[str, Any]:
        """Search messages with the Graph $search parameter."""
        return await with_retry(
            lambda: self.http_client.search_messages(
                query,
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
                top=top,
            ),
            max_retries=3,
        )

    async def get_normalized_message(self, message_id: str) -> NormalizedDocument:
        """Fetch a message and return it as a NormalizedDocument."""
        raw = await self.get_message(message_id)
        return normalize_message(raw, self.connector_id, self.tenant_id)
