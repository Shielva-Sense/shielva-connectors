"""Gmail connector — orchestration only.

Delegates ALL HTTP calls to client/http_client.py.
Delegates ALL normalization to helpers/normalizer.py.
Owns ALL OAuth token-refresh logic (on_token_refresh).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp
import structlog
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
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
from exceptions import GmailAPIError, GmailAuthError, GmailRateLimitError
from helpers import normalizer
from helpers.utils import build_after_query

logger = structlog.get_logger(__name__)


class GmailConnector(BaseConnector):
    CONNECTOR_TYPE = "google_gmail_connector"
    CONNECTOR_NAME = "Google Gmail"
    AUTH_TYPE = "oauth2_code"
    AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URI = "https://oauth2.googleapis.com/token"
    REQUIRED_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

    # OCP: config keys declared as class constant — install() validates from this list
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

    # OCP: status-code → (health, auth_status) lookup; health_check() reads this dict
    _STATUS_MAP: Dict[int, tuple] = {
        401: (ConnectorHealth.UNHEALTHY, AuthStatus.TOKEN_EXPIRED),
        403: (ConnectorHealth.UNHEALTHY, AuthStatus.MISSING_CREDENTIALS),
        429: (ConnectorHealth.DEGRADED, AuthStatus.CONNECTED),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.scopes: str = self.config.get("scopes", "")
        self.auth_url: str = self.config.get("auth_url", "")
        self.token_url: str = self.config.get("token_url", "")
        self.base_url: str = self.config.get("base_url", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", "")
        self.pagination_type: str = self.config.get("pagination_type", "")
        self.api_version: str = self.config.get("api_version", "v1")

    # ── Internal helpers ───────────────────────────────────────────────────

    def _effective_token_uri(self) -> str:
        return self.config.get("token_url") or self.TOKEN_URI

    def _effective_api_version(self) -> str:
        return self.config.get("api_version") or "v1"

    async def _build_http_client(self) -> GmailHTTPClient:
        """Return an http_client backed by a valid (possibly just-refreshed) access token."""
        token_info = await self.ensure_token()
        return GmailHTTPClient(
            access_token=token_info.access_token,
            api_version=self._effective_api_version(),
        )

    # ── BaseConnector abstract methods ─────────────────────────────────────

    async def install(self, config: Dict[str, Any] = None) -> ConnectorStatus:
        """Validate install config and persist it."""
        cfg = config or {}
        if not cfg.get("client_id"):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id is required",
            )
        if not cfg.get("client_secret"):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_secret is required",
            )
        await self.save_config(cfg)
        self.client_id = self.config.get("client_id", "")
        self.client_secret = self.config.get("client_secret", "")
        self.api_version = self.config.get("api_version", "v1")
        logger.info("gmail.install.ok", connector_id=self.connector_id, tenant_id=self.tenant_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Authorization required — click Authorize to continue",
        )

    async def authorize(self, auth_data: Dict[str, Any]) -> TokenInfo:
        """Exchange an OAuth2 authorization code for access + refresh tokens."""
        code: str = auth_data.get("code", "")
        redirect_uri: str = self.config.get("redirect_uri", "")
        token_uri = self._effective_token_uri()

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.config.get("client_id", ""),
            "client_secret": self.config.get("client_secret", ""),
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(token_uri, data=payload) as resp:
                resp.raise_for_status()
                data: Dict[str, Any] = await resp.json()

        expires_in: int = int(data.get("expires_in", 3600))
        scope_str: str = data.get("scope", "")
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=scope_str.split() if scope_str else list(self.REQUIRED_SCOPES),
            raw=data,
        )
        await self.set_token(token_info)
        logger.info("gmail.authorize.ok", connector_id=self.connector_id, scopes=token_info.scopes)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify token validity by calling users.getProfile."""
        try:
            http = await self._build_http_client()
            profile = await http.execute_get_profile()
            email = profile.get("emailAddress", "")
            logger.info("gmail.health_check.ok", email=email)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected as {email}",
                metadata={"email": email},
            )
        except GmailAuthError as exc:
            health, auth_status = self._STATUS_MAP.get(
                401, (ConnectorHealth.UNHEALTHY, AuthStatus.TOKEN_EXPIRED)
            )
            logger.warning("gmail.health_check.auth_error", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=health,
                auth_status=auth_status,
                message=str(exc),
            )
        except Exception as exc:
            logger.error("gmail.health_check.failed", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Fetch and ingest Gmail messages — incremental or full."""
        logger.info(
            "gmail.sync.start",
            connector_id=self.connector_id,
            full=full,
            since=since.isoformat() if since else None,
        )
        try:
            query: Optional[str] = None
            if not full and since is not None:
                query = build_after_query(since)

            raw_messages = await self.list_email(query=query)
            documents = normalizer.normalize_batch(
                raw_messages,
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
            )

            documents_found = len(raw_messages)
            documents_failed = documents_found - len(documents)
            documents_synced = 0

            if documents:
                await self.ingest_batch(documents, kb_id=kb_id or "", webhook_url=webhook_url)
                documents_synced = len(documents)

            await self.report_status(
                kb_id=kb_id or "",
                status="completed",
                details=f"Synced {documents_synced} emails",
                docs_count=documents_synced,
                webhook_url=webhook_url,
            )

            logger.info(
                "gmail.sync.completed",
                found=documents_found,
                synced=documents_synced,
                failed=documents_failed,
            )
            return SyncResult(
                status=SyncStatus.COMPLETED,
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                completed_at=datetime.utcnow(),
            )
        except (GmailAuthError, GmailRateLimitError, GmailAPIError) as exc:
            logger.error("gmail.sync.failed", error=str(exc))
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                message=str(exc),
                completed_at=datetime.utcnow(),
            )
        except Exception as exc:
            logger.error("gmail.sync.unexpected_error", error=str(exc))
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                message=str(exc),
                completed_at=datetime.utcnow(),
            )

    # ── User-requested standalone methods ─────────────────────────────────

    async def list_email(
        self,
        label_ids: Optional[List[str]] = None,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch all matching email metadata with full pagination.

        Returns a flat list of raw message dicts (id, threadId, snippet, payload).
        """
        if label_ids is None:
            label_ids = ["INBOX", "UNREAD"]

        http = await self._build_http_client()
        all_messages: List[Dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            page = await http.execute_list_messages(
                label_ids=label_ids,
                page_token=page_token,
                max_results=100,
                query=query,
            )
            stubs: List[Dict[str, str]] = page.get("messages", [])
            if not stubs:
                break

            for stub in stubs:
                try:
                    full_msg = await http.execute_get_message(
                        msg_id=stub["id"],
                        format="metadata",
                        metadata_headers=["Subject", "From", "Date"],
                    )
                    all_messages.append(full_msg)
                except Exception as exc:
                    logger.warning(
                        "gmail.list_email.skip_message",
                        msg_id=stub.get("id"),
                        error=str(exc),
                    )

            page_token = page.get("nextPageToken")
            if not page_token:
                break

        logger.info("gmail.list_email.done", count=len(all_messages))
        return all_messages

    async def on_token_refresh(self) -> TokenInfo:
        """Refresh the OAuth2 access token. Sole owner of token refresh logic."""
        token_info = await self.get_token()
        if not token_info or not token_info.refresh_token:
            raise GmailAuthError("No refresh token available — re-authorize required")

        creds = Credentials(
            token=token_info.access_token,
            refresh_token=token_info.refresh_token,
            token_uri=self._effective_token_uri(),
            client_id=self.config.get("client_id", ""),
            client_secret=self.config.get("client_secret", ""),
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, creds.refresh, GoogleAuthRequest())

        new_token = TokenInfo(
            access_token=creds.token,
            refresh_token=creds.refresh_token or token_info.refresh_token,
            expires_at=creds.expiry.replace(tzinfo=None) if creds.expiry else None,
            token_type="Bearer",
            scopes=list(creds.scopes) if creds.scopes else token_info.scopes,
        )
        await self.set_token(new_token)
        logger.info("gmail.token.refreshed", connector_id=self.connector_id)
        return new_token
