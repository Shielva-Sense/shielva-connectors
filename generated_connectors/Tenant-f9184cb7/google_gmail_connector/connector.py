"""GmailConnector — orchestration only; zero raw HTTP, zero JSON parsing."""
import os
from datetime import datetime, timezone, timedelta
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

from client.http_client import GmailHTTPClient
from exceptions import ConnectorAuthError, ConnectorError, ConnectorPermissionError
from helpers.normalizer import normalize_message
from helpers.utils import load_known_ids, save_known_ids, build_mime_raw
from models import BulkDeleteResult

logger = structlog.get_logger(__name__)


class GmailConnector(BaseConnector):
    """Shielva connector for Google Gmail — supports list, read, delete, sync."""

    # ── Connector identity ──────────────────────────────────────────────────
    CONNECTOR_TYPE = "google_gmail"
    CONNECTOR_NAME = "Google Gmail"
    AUTH_TYPE = "oauth2_code"

    # ── OAuth2 provider endpoints (provider-wide; BaseConnector.get_oauth_url
    #    reads AUTH_URI to build the consent URL, authorize() uses TOKEN_URI) ──
    AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URI = "https://oauth2.googleapis.com/token"
    REQUIRED_SCOPES = [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
    ]

    # ── Provider-wide hardcoded constants (same for every tenant) ───────────
    RATE_LIMIT_PER_MIN = 250
    PAGINATION_TYPE = "cursor"
    API_VERSION = "v1"
    ALLOW_PERMANENT_DELETE = False

    # ── OCP: status-code → exception mapping (shared with http_client) ──────
    _STATUS_MAP: Dict[int, str] = {
        401: "auth",
        403: "permission",
        404: "not_found",
        429: "rate_limit",
    }

    # ── Config keys read from user-supplied install fields ──────────────────
    REQUIRED_CONFIG_KEYS: List[str] = ["allow_permanent_delete", "client_id", "client_secret"]

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        # Only user-provided (bind:false) fields read from self.config:
        self.allow_permanent_delete: bool = bool(
            self.config.get("allow_permanent_delete", False)
        )

    # ── Private helpers ─────────────────────────────────────────────────────

    async def _build_http_client(self) -> GmailHTTPClient:
        """Ensure a valid token and return a ready GmailHTTPClient."""
        token = await self.ensure_token()
        return GmailHTTPClient(
            access_token=token.access_token,
            base_url=self.config.get("base_url", "https://gmail.googleapis.com/gmail/v1"),
        )

    def _assert_permanent_delete_allowed(self) -> None:
        """Raise ConnectorPermissionError if permanent delete is disabled."""
        if not bool(self.config.get("allow_permanent_delete", False)):
            raise ConnectorPermissionError(
                "Permanent delete is disabled; set allow_permanent_delete=True "
                "and include https://mail.google.com/ in scopes."
            )

    async def _remove_from_kb(self, msg_id: str) -> None:
        """Call the platform KB document-removal API for a single message."""
        ingestion_url = os.getenv("INGESTION_URL", "http://localhost:8000")
        url = f"{ingestion_url}/remove"
        payload = {
            "tenant_id": self.tenant_id,
            "connector_id": self.connector_id,
            "doc_id": msg_id,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status not in (200, 204):
                        text = await resp.text()
                        logger.warning(
                            "gmail.remove_from_kb.warn",
                            msg_id=msg_id,
                            status=resp.status,
                            body=text[:200],
                        )
        except Exception as exc:
            logger.warning("gmail.remove_from_kb.error", msg_id=msg_id, error=str(exc))

    # ── BaseConnector abstract methods ──────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate config and return a PENDING ConnectorStatus (no API call)."""
        if not self.config.get("client_id") or not self.config.get("client_secret"):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="client_id and client_secret are required install fields",
            )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Gmail connector installed. Complete OAuth flow to activate.",
        )

    async def authorize(self, auth_code: str, state: Optional[str] = None) -> TokenInfo:
        """Exchange OAuth authorization code for access + refresh tokens."""
        redirect_uri = self.config.get("redirect_uri", "")
        token_endpoint = self.config.get("token_url", "https://oauth2.googleapis.com/token")
        payload = {
            "code": auth_code,
            "client_id": self.config.get("client_id"),
            "client_secret": self.config.get("client_secret"),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(token_endpoint, data=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ConnectorAuthError(f"Token exchange failed: {text}")
                data: Dict[str, Any] = await resp.json(content_type=None)

        scopes_raw = data.get("scope", "")
        scopes = scopes_raw.split() if isinstance(scopes_raw, str) else list(scopes_raw)
        expires_in = int(data.get("expires_in", 3600))
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in)

        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=expires_at,
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )
        await self.set_token(token_info)
        return token_info

    async def on_token_refresh(self) -> TokenInfo:
        """Refresh an expired access token using the stored refresh token."""
        current = self._token_info
        if not current or not current.refresh_token:
            raise ConnectorAuthError("No refresh token available.")
        token_endpoint = self.config.get("token_url", "https://oauth2.googleapis.com/token")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": current.refresh_token,
            "client_id": self.config.get("client_id"),
            "client_secret": self.config.get("client_secret"),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(token_endpoint, data=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ConnectorAuthError(f"Token refresh failed: {text}")
                data: Dict[str, Any] = await resp.json(content_type=None)

        scopes_raw = data.get("scope", "")
        scopes = scopes_raw.split() if isinstance(scopes_raw, str) else list(scopes_raw)
        expires_in = int(data.get("expires_in", 3600))
        import datetime as dt
        expires_at = dt.datetime.utcnow() + dt.timedelta(seconds=expires_in)
        return TokenInfo(
            access_token=data["access_token"],
            refresh_token=current.refresh_token,
            expires_at=expires_at,
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify token validity by calling users/me/profile."""
        try:
            client = await self._build_http_client()
            profile = await client.execute_get_profile()
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected as {profile.get('emailAddress', 'unknown')}",
            )
        except ConnectorAuthError:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message="Access token expired — re-authorize.",
            )
        except ConnectorPermissionError:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="Insufficient scopes.",
            )
        except Exception as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: str = "",
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Incremental or full sync with deletion propagation."""
        client = await self._build_http_client()
        known_ids = load_known_ids(self.config)

        query = ""
        if since and not full:
            unix_ts = int(since.timestamp())
            query = f"after:{unix_ts}"

        current_ids: set = set()
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        page_token: Optional[str] = None

        try:
            while True:
                page = await client.execute_list_messages(
                    query=query, max_results=100, page_token=page_token
                )
                stubs = page.get("messages", [])
                documents_found += len(stubs)

                for stub in stubs:
                    msg_id = stub["id"]
                    current_ids.add(msg_id)
                    try:
                        raw = await client.execute_get_message(msg_id)
                        doc = normalize_message(raw, self.tenant_id, self.connector_id)
                        await self.ingest_document(doc, kb_id=kb_id, webhook_url=webhook_url)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("gmail.sync.message_error", msg_id=msg_id, error=str(exc))
                        documents_failed += 1

                page_token = page.get("nextPageToken")
                if not page_token:
                    break

            # Propagate deletions
            removed_ids = known_ids - current_ids
            for msg_id in removed_ids:
                await self._remove_from_kb(msg_id)
            if removed_ids:
                logger.info(
                    "gmail.sync.deletions_propagated",
                    count=len(removed_ids),
                    connector_id=self.connector_id,
                )

            # Persist updated known IDs
            await self.save_config(save_known_ids(self.config, current_ids))

            status = SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Sync complete. Removed {len(removed_ids)} stale IDs.",
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

    # ── User-requested public methods ────────────────────────────────────────

    async def list_email(
        self,
        query: str = "",
        max_results: int = 100,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List messages matching *query*; returns raw page dict with nextPageToken."""
        client = await self._build_http_client()
        return await client.execute_list_messages(
            query=query, max_results=max_results, page_token=page_token
        )

    async def read_email(self, msg_id: str) -> NormalizedDocument:
        """Fetch a single message by ID and return a NormalizedDocument."""
        client = await self._build_http_client()
        raw = await client.execute_get_message(msg_id)
        return normalize_message(raw, self.tenant_id, self.connector_id)

    async def add_email(
        self,
        msg_id: str,
        label_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Apply *label_ids* to message *msg_id* via messages.modify."""
        client = await self._build_http_client()
        return await client.execute_modify_message(
            msg_id=msg_id, add_label_ids=label_ids or []
        )

    async def move_email(
        self,
        msg_id: str,
        destination_label_id: str,
        remove_label_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Move message *msg_id* to *destination_label_id*, removing *remove_label_ids*."""
        client = await self._build_http_client()
        result = await client.execute_modify_message(
            msg_id=msg_id,
            add_label_ids=[destination_label_id],
            remove_label_ids=remove_label_ids or ["INBOX"],
        )
        logger.info("gmail.move_email.ok", msg_id=msg_id, connector_id=self.connector_id)
        return result

    async def update_email(
        self,
        msg_id: str,
        add_label_ids: Optional[List[str]] = None,
        remove_label_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Add and/or remove labels on message *msg_id* via messages.modify."""
        client = await self._build_http_client()
        result = await client.execute_modify_message(
            msg_id=msg_id,
            add_label_ids=add_label_ids or [],
            remove_label_ids=remove_label_ids or [],
        )
        logger.info("gmail.update_email.ok", msg_id=msg_id, connector_id=self.connector_id)
        return result

    async def get_email(self, msg_id: str) -> NormalizedDocument:
        """Fetch a single message by ID and return a NormalizedDocument."""
        client = await self._build_http_client()
        raw = await client.execute_get_message(msg_id)
        return normalize_message(raw, self.tenant_id, self.connector_id)

    async def delete_email(self, msg_id: str, permanent: bool = False) -> Any:
        """Alias for delete_message — kept for API surface consistency."""
        return await self.delete_message(msg_id, permanent=permanent)

    async def remove_email(self, msg_id: str) -> Any:
        """Soft-delete (trash) alias — kept for API surface consistency."""
        return await self.delete_message(msg_id, permanent=False)

    async def delete_message(self, msg_id: str, permanent: bool = False) -> Any:
        """Trash or permanently delete a single message.

        permanent=False → POST messages/{id}/trash (recoverable)
        permanent=True  → DELETE messages/{id} (unrecoverable — requires allow_permanent_delete)
        """
        client = await self._build_http_client()
        if permanent:
            self._assert_permanent_delete_allowed()
            result = await client.execute_delete_message(msg_id)
        else:
            result = await client.execute_trash_message(msg_id)
        logger.info(
            "gmail.delete_message.ok",
            msg_id=msg_id,
            permanent=permanent,
            connector_id=self.connector_id,
        )
        return result

    async def delete_thread(self, thread_id: str, permanent: bool = False) -> Any:
        """Trash or permanently delete an entire thread.

        permanent=False → POST threads/{id}/trash
        permanent=True  → DELETE threads/{id} (requires allow_permanent_delete)
        """
        client = await self._build_http_client()
        if permanent:
            self._assert_permanent_delete_allowed()
            result = await client.execute_delete_thread(thread_id)
        else:
            result = await client.execute_trash_thread(thread_id)
        logger.info(
            "gmail.delete_thread.ok",
            thread_id=thread_id,
            permanent=permanent,
            connector_id=self.connector_id,
        )
        return result

    async def bulk_delete(
        self, query: str, permanent: bool = False
    ) -> BulkDeleteResult:
        """Delete all messages matching *query*.

        Collects all message IDs via the pageToken loop first, then deletes
        each individually so pagination is not corrupted by mid-loop mutations.
        Per-message errors are caught and counted; the loop never aborts early.
        """
        if permanent:
            self._assert_permanent_delete_allowed()

        client = await self._build_http_client()

        # Collect IDs first
        all_ids: List[str] = []
        page_token: Optional[str] = None
        while True:
            page = await client.execute_list_messages(
                query=query, max_results=100, page_token=page_token
            )
            for stub in page.get("messages", []):
                all_ids.append(stub["id"])
            page_token = page.get("nextPageToken")
            if not page_token:
                break

        # Delete each message
        result = BulkDeleteResult()
        errors: List[str] = []
        for msg_id in all_ids:
            try:
                if permanent:
                    await client.execute_delete_message(msg_id)
                else:
                    await client.execute_trash_message(msg_id)
                result.deleted += 1
            except Exception as exc:
                result.failed += 1
                errors.append(f"{msg_id}: {exc}")

        result.errors = errors
        logger.info(
            "gmail.bulk_delete.ok",
            deleted=result.deleted,
            failed=result.failed,
            permanent=permanent,
            connector_id=self.connector_id,
        )
        return result

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build an RFC 2822 MIME message and send it via POST /users/me/messages/send.

        Requires the gmail.send OAuth scope — call returns {id, threadId} on success.
        Raises ConnectorPermissionError if the scope is absent.
        """
        raw = build_mime_raw(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        client = await self._build_http_client()
        result = await client.execute_send_message(raw)
        logger.info(
            "gmail.send_email.ok",
            to=to,
            subject=subject,
            connector_id=self.connector_id,
        )
        return result

    async def post_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Public alias for send_email() — same behaviour, different API surface name."""
        return await self.send_email(to=to, subject=subject, body=body, cc=cc, bcc=bcc)

    async def modify_message(
        self,
        msg_id: str,
        add_label_ids: Optional[List[str]] = None,
        remove_label_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Add and/or remove label IDs on message *msg_id* via messages.modify."""
        client = await self._build_http_client()
        result = await client.execute_modify_message(
            msg_id=msg_id,
            add_label_ids=add_label_ids or [],
            remove_label_ids=remove_label_ids or [],
        )
        logger.info(
            "gmail.modify_message.ok",
            msg_id=msg_id,
            connector_id=self.connector_id,
        )
        return result

    async def disconnect(self) -> None:
        """Clear stored tokens and reset auth status."""
        await self.clear_token()
        logger.info("gmail.disconnect", connector_id=self.connector_id)
