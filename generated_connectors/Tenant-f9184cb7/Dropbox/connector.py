"""Dropbox connector — orchestration only.

All HTTP calls → ``client/http_client.py``
All normalization → ``helpers/normalizer.py``
All utilities → ``helpers/utils.py``

Auth: OAuth 2.0 Authorization Code Grant. Access tokens expire after 4 hours;
refresh tokens are minted at first authorize when ``token_access_type=offline``.
The BaseConnector ``ensure_token()`` machinery calls ``on_token_refresh()``
ahead of expiry.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
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

from client.http_client import DropboxHTTPClient
from exceptions import (
    DropboxAuthError,
    DropboxError,
    DropboxNetworkError,
    DropboxNotFoundError,
    DropboxRateLimitError,
)
from helpers.normalizer import normalize_entry
from helpers.utils import utcnow, with_retry

logger = structlog.get_logger(__name__)


class DropboxConnector(BaseConnector):
    """Shielva connector for the Dropbox REST API v2 (Files + Sharing + Users)."""

    CONNECTOR_TYPE = "dropbox"
    CONNECTOR_NAME = "Dropbox"
    AUTH_TYPE = "oauth2_code"

    # Provider-wide OAuth2 endpoints (class constants — BaseConnector reads these).
    AUTH_URI = "https://www.dropbox.com/oauth2/authorize"
    TOKEN_URI = "https://api.dropboxapi.com/oauth2/token"
    REVOKE_URI = "https://api.dropboxapi.com/2/auth/token/revoke"

    REQUIRED_SCOPES: List[str] = [
        "files.metadata.read",
        "files.content.read",
        "files.content.write",
        "sharing.read",
        "sharing.write",
        "account_info.read",
    ]

    REQUIRED_CONFIG_KEYS: List[str] = ["client_id", "client_secret"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    _SYNC_PAGE_LIMIT = 200

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        # Per-tenant user-supplied credentials — NEVER hardcoded.
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        # Gateway injects redirect_uri before calling authorize().
        self.redirect_uri: str = self.config.get("redirect_uri", "")

        access_token = self.config.get("access_token", "")
        self.http_client = DropboxHTTPClient(access_token=access_token)

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _ensure_http_token(self) -> None:
        """Sync the access token from ``self._token_info`` into the HTTP client.

        BaseConnector owns token storage (Redis). We rebind the client header
        on every call to pick up refreshes from ``on_token_refresh``.
        """
        token = await self.get_token()
        if token and token.access_token:
            self.http_client.set_access_token(token.access_token)

    def _classify_failure(self, exc: Exception) -> ConnectorStatus:
        """OCP — map any exception to a ``ConnectorStatus`` via ``_STATUS_MAP``."""
        status = getattr(exc, "status_code", 0)
        health_name, auth_name = self._STATUS_MAP.get(status, ("DEGRADED", "FAILED"))
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth[health_name],
            auth_status=AuthStatus[auth_name],
            message=str(exc),
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time credentials and persist them.

        ``install()`` does NOT call the Dropbox API — the OAuth code exchange
        happens in ``authorize()``. We only check ``client_id`` + ``client_secret``
        are non-empty so the gateway can render the authorization URL.
        """
        missing = [k for k in self.REQUIRED_CONFIG_KEYS if not self.config.get(k)]
        if missing:
            logger.warning(
                "dropbox.install.missing_credentials",
                connector_id=self.connector_id,
                missing=missing,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required config keys: {', '.join(missing)}",
            )

        await self.save_config({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.config.get("redirect_uri", ""),
        })
        logger.info("dropbox.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Dropbox connector installed — authorize() next",
            metadata={"requires_oauth_redirect": True},
        )

    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Exchange the OAuth ``auth_code`` for an access + refresh token.

        ``redirect_uri`` MUST come from ``self.config`` (the gateway injects it
        before calling). Never derive it from ``state`` (which carries the
        ``connector_id``), and never hardcode it — both produce a 400
        ``invalid_request`` at the token exchange.
        """
        if not auth_code:
            raise DropboxAuthError("authorize() requires a non-empty auth_code")

        redirect_uri = self.config.get("redirect_uri", "") or self.redirect_uri
        if not redirect_uri:
            raise DropboxAuthError(
                "authorize() requires redirect_uri in self.config — gateway should inject it"
            )

        token = await self._exchange_token({
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
        })
        await self.set_token(token)
        self.http_client.set_access_token(token.access_token)
        logger.info(
            "dropbox.authorize.ok",
            connector_id=self.connector_id,
            has_refresh=bool(token.refresh_token),
        )
        return token

    async def on_token_refresh(self) -> Optional[TokenInfo]:
        """Refresh the access token using the stored refresh token.

        Called by ``BaseConnector.ensure_token()`` ahead of expiry.
        """
        existing = await self.get_token()
        if not existing or not existing.refresh_token:
            return None
        token = await self._exchange_token({
            "grant_type": "refresh_token",
            "refresh_token": existing.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        # Dropbox refresh-token responses re-use the existing refresh token if a
        # new one is not returned. Preserve it.
        if not token.refresh_token:
            token.refresh_token = existing.refresh_token
        await self.set_token(token)
        self.http_client.set_access_token(token.access_token)
        return token

    async def _exchange_token(self, params: Dict[str, str]) -> TokenInfo:
        """Hit ``TOKEN_URI`` with ``application/x-www-form-urlencoded`` payload."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.TOKEN_URI,
                    data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise DropboxNetworkError(f"Token exchange transport error: {exc}") from exc

        if response.status_code != 200:
            try:
                body = response.json()
            except Exception:
                body = {"raw": response.text}
            raise DropboxAuthError(
                f"Token exchange failed: HTTP {response.status_code}",
                status_code=response.status_code,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )

        try:
            payload = response.json()
        except Exception as exc:
            raise DropboxAuthError(f"Token endpoint returned non-JSON: {exc}") from exc

        expires_in = int(payload.get("expires_in", 14400))
        scopes_str = payload.get("scope", "") or ""
        return TokenInfo(
            access_token=payload.get("access_token", ""),
            refresh_token=payload.get("refresh_token"),
            expires_at=utcnow() + timedelta(seconds=expires_in),
            token_type=payload.get("token_type", "Bearer"),
            scopes=scopes_str.split() if scopes_str else [],
            raw=payload,
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Dropbox API connectivity by calling ``/users/get_current_account``."""
        await self._ensure_http_token()
        try:
            account = await with_retry(
                self.http_client.get_current_account,
                max_retries=2,
            )
            email = (account or {}).get("email", "") if isinstance(account, dict) else ""
            display_name = ""
            name_obj = (account or {}).get("name")
            if isinstance(name_obj, dict):
                display_name = name_obj.get("display_name", "")
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Dropbox API reachable",
                metadata={"email": email, "display_name": display_name},
            )
        except DropboxAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Dropbox auth failed: {exc}",
            )
        except DropboxRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Dropbox rate limited: {exc}",
            )
        except DropboxNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Dropbox network error: {exc}",
            )
        except DropboxError as exc:
            return self._classify_failure(exc)

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Walk the user's Dropbox recursively and ingest every file/folder.

        Uses ``list_folder(path="", recursive=True)`` + ``list_folder_continue``
        for cursor-based pagination. Dropbox has no server-side ``since``
        filter on list_folder; incremental sync requires re-using a stored
        cursor across runs (left as a follow-up — emits the warning and falls
        back to full sync today).
        """
        _ = since, full  # documented above
        await self._ensure_http_token()

        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            entries = await self._fetch_all_entries()
        except DropboxError as exc:
            logger.error(
                "dropbox.sync.fetch_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

        documents_found = len(entries)
        for raw in entries:
            try:
                doc: NormalizedDocument = normalize_entry(
                    raw, self.connector_id, self.tenant_id
                )
                await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                documents_synced += 1
            except Exception as exc:  # noqa: BLE001 — keep the loop alive on a single bad doc
                logger.error(
                    "dropbox.sync.entry_failed",
                    connector_id=self.connector_id,
                    error=str(exc),
                )
                documents_failed += 1

        status = SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=documents_found,
            documents_synced=documents_synced,
            documents_failed=documents_failed,
            message=f"Synced {documents_synced}/{documents_found} Dropbox entries",
        )

    async def _fetch_all_entries(self) -> List[Dict[str, Any]]:
        """Drain ``list_folder`` (cursor pagination) into a flat list."""
        entries: List[Dict[str, Any]] = []
        page = await with_retry(
            self.http_client.list_folder,
            path="",
            recursive=True,
            limit=self._SYNC_PAGE_LIMIT,
            max_retries=3,
        )
        entries.extend(page.get("entries", []) or [])
        while page.get("has_more"):
            cursor = page.get("cursor", "")
            if not cursor:
                break
            page = await with_retry(
                self.http_client.list_folder_continue,
                cursor,
                max_retries=3,
            )
            entries.extend(page.get("entries", []) or [])
        return entries

    async def disconnect(self) -> ConnectorStatus:
        """Revoke the current Dropbox access token + drop local state."""
        await self._ensure_http_token()
        try:
            await self.http_client.token_revoke()
        except DropboxAuthError:
            # Already invalid — fine, fall through.
            pass
        await self.clear_token()
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.UNAUTHENTICATED,
            message="Dropbox token revoked",
        )

    # ── Public API methods (per implementation_plan.md Section 5) ──────────

    async def list_folder(
        self,
        path: str = "",
        recursive: bool = False,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """POST /files/list_folder."""
        await self._ensure_http_token()
        return await with_retry(
            self.http_client.list_folder,
            path=path,
            recursive=recursive,
            limit=limit,
            max_retries=3,
        )

    async def list_folder_continue(self, cursor: str) -> Dict[str, Any]:
        """POST /files/list_folder/continue."""
        await self._ensure_http_token()
        return await with_retry(
            self.http_client.list_folder_continue,
            cursor,
            max_retries=3,
        )

    async def get_metadata(self, path: str) -> Dict[str, Any]:
        """POST /files/get_metadata.

        Note: this OVERRIDES the inherited ``BaseConnector.get_metadata(key)``
        (a checkpoint helper). Dropbox's metadata endpoint is path-keyed and
        returns the full file/folder metadata dict.
        """
        await self._ensure_http_token()
        return await with_retry(
            self.http_client.get_metadata,
            path,
            max_retries=3,
        )

    async def download_file(self, path: str) -> Dict[str, Any]:
        """POST https://content.dropboxapi.com/2/files/download."""
        await self._ensure_http_token()
        return await with_retry(
            self.http_client.download_file,
            path,
            max_retries=3,
        )

    async def upload_file(
        self,
        path: str,
        content: bytes,
        mode: str = "add",
        autorename: bool = False,
    ) -> Dict[str, Any]:
        """POST https://content.dropboxapi.com/2/files/upload."""
        await self._ensure_http_token()
        return await self.http_client.upload_file(
            path=path, content=content, mode=mode, autorename=autorename,
        )

    async def copy_file(
        self,
        from_path: str,
        to_path: str,
        autorename: bool = False,
    ) -> Dict[str, Any]:
        """POST /files/copy_v2."""
        await self._ensure_http_token()
        return await self.http_client.copy_file(
            from_path=from_path, to_path=to_path, autorename=autorename,
        )

    async def move_file(
        self,
        from_path: str,
        to_path: str,
        autorename: bool = False,
    ) -> Dict[str, Any]:
        """POST /files/move_v2."""
        await self._ensure_http_token()
        return await self.http_client.move_file(
            from_path=from_path, to_path=to_path, autorename=autorename,
        )

    async def delete_file(self, path: str) -> Dict[str, Any]:
        """POST /files/delete_v2."""
        await self._ensure_http_token()
        return await self.http_client.delete_file(path)

    async def create_folder(
        self,
        path: str,
        autorename: bool = False,
    ) -> Dict[str, Any]:
        """POST /files/create_folder_v2."""
        await self._ensure_http_token()
        return await self.http_client.create_folder(path=path, autorename=autorename)

    async def search(
        self,
        query: str,
        max_results: int = 100,
        path: str = "",
    ) -> Dict[str, Any]:
        """POST /files/search_v2."""
        await self._ensure_http_token()
        return await with_retry(
            self.http_client.search,
            query,
            max_results=max_results,
            path=path,
            max_retries=3,
        )

    async def list_revisions(self, path: str, limit: int = 10) -> Dict[str, Any]:
        """POST /files/list_revisions."""
        await self._ensure_http_token()
        return await with_retry(
            self.http_client.list_revisions,
            path,
            limit=limit,
            max_retries=3,
        )

    async def restore_revision(self, path: str, rev: str) -> Dict[str, Any]:
        """POST /files/restore."""
        await self._ensure_http_token()
        return await self.http_client.restore_revision(path=path, rev=rev)

    async def create_shared_link(
        self,
        path: str,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /sharing/create_shared_link_with_settings."""
        await self._ensure_http_token()
        return await self.http_client.create_shared_link(path=path, settings=settings)

    async def list_shared_links(
        self,
        path: Optional[str] = None,
        cursor: Optional[str] = None,
        direct_only: bool = True,
    ) -> Dict[str, Any]:
        """POST /sharing/list_shared_links."""
        await self._ensure_http_token()
        return await with_retry(
            self.http_client.list_shared_links,
            path=path,
            cursor=cursor,
            direct_only=direct_only,
            max_retries=3,
        )

    async def get_current_account(self) -> Dict[str, Any]:
        """POST /users/get_current_account."""
        await self._ensure_http_token()
        return await with_retry(
            self.http_client.get_current_account,
            max_retries=3,
        )

    async def get_account(self, account_id: str) -> Dict[str, Any]:
        """POST /users/get_account."""
        await self._ensure_http_token()
        return await with_retry(
            self.http_client.get_account,
            account_id,
            max_retries=3,
        )

    async def get_space_usage(self) -> Dict[str, Any]:
        """POST /users/get_space_usage."""
        await self._ensure_http_token()
        return await with_retry(
            self.http_client.get_space_usage,
            max_retries=3,
        )
