"""Box connector — orchestration only.

All HTTP calls  → client/http_client.py
All normalization + retry → helpers/utils.py
All models (standalone) → models.py

Imports BaseConnector via a try/except guard so this module loads cleanly
in the gateway's AST sandbox even when the Shielva SDK is absent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import structlog

try:
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
    _BASE = BaseConnector
    _HAS_SDK = True
except ImportError:
    _BASE = object  # type: ignore[assignment,misc]
    _HAS_SDK = False

from client.http_client import BoxHTTPClient
from exceptions import (
    BoxAuthError,
    BoxError,
    BoxNetworkError,
)
from helpers.utils import normalize_file, normalize_folder, with_retry
from models import (
    AuthStatus as _LocalAuthStatus,
    ConnectorHealth as _LocalConnectorHealth,
    ConnectorDocument,
    HealthCheckResult,
    InstallResult,
    SyncResult as _LocalSyncResult,
    SyncStatus as _LocalSyncStatus,
)

logger = structlog.get_logger(__name__)

_BOX_BASE = "https://api.box.com/2.0"
_AUTH_URI = "https://account.box.com/api/oauth2/authorize"
_TOKEN_URI = "https://api.box.com/api/oauth2/token"


class BoxConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for the Box Content API (v2)."""

    CONNECTOR_TYPE = "box"
    CONNECTOR_NAME = "Box"
    AUTH_TYPE = "oauth2"
    AUTH_URI = _AUTH_URI
    TOKEN_URI = _TOKEN_URI

    REQUIRED_SCOPES: List[str] = [
        "root_readonly",
    ]

    REQUIRED_CONFIG_KEYS = [
        "client_id",
        "client_secret",
    ]

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        if _HAS_SDK:
            super().__init__(tenant_id, connector_id, cfg)
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = cfg
            self._token_info: Optional[Any] = None

        self._http_client: Optional[BoxHTTPClient] = None

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> BoxHTTPClient:
        """Return (and lazily create) the HTTP client."""
        if self._http_client is None:
            self._http_client = BoxHTTPClient(base_url=_BOX_BASE)
        return self._http_client

    async def _get_valid_token(self) -> str:
        """Return a valid access token, refreshing if the SDK is available."""
        if _HAS_SDK:
            token_info = await self.ensure_token()
            return token_info.access_token  # type: ignore[return-value]
        if self._token_info:
            if isinstance(self._token_info, dict):
                return self._token_info.get("access_token", "")  # type: ignore[return-value]
            return getattr(self._token_info, "access_token", "")
        return ""

    # ── SDK hook: token refresh ───────────────────────────────────────────────

    if _HAS_SDK:
        async def on_token_refresh(self) -> "TokenInfo":  # type: ignore[override]
            """Refresh the OAuth2 access token using the stored refresh token."""
            if not self._token_info or not self._token_info.refresh_token:
                raise RefreshError("No refresh token available")  # type: ignore[name-defined]

            client_id = self.config.get("client_id", "")
            client_secret = self.config.get("client_secret", "")
            stored_token = self._token_info.refresh_token

            data = await self._ensure_client().post_form_data(
                url=_TOKEN_URI,
                payload={
                    "grant_type": "refresh_token",
                    "refresh_token": stored_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                context="on_token_refresh",
            )

            expires_in = int(data.get("expires_in", 3600))
            new_scopes = (
                data.get("scope", "").split()
                if data.get("scope")
                else list(self._token_info.scopes)
            )
            return TokenInfo(  # type: ignore[name-defined]
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token") or stored_token,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
                token_type=data.get("token_type", "Bearer"),
                scopes=new_scopes,
            )

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        """Validate install-time config and return connector/install status.

        Returns ConnectorStatus (when SDK present) or InstallResult (standalone).
        Missing client_id or client_secret → MISSING_CREDENTIALS result.
        """
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        if not client_id or not client_secret:
            logger.warning(
                "box.install.missing_credentials",
                connector_id=self.connector_id,
            )
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,  # type: ignore[name-defined]
                    auth_status=AuthStatus.MISSING_CREDENTIALS,  # type: ignore[name-defined]
                    message="client_id and client_secret are required",
                )
            return InstallResult(
                health=_LocalConnectorHealth.OFFLINE,
                auth_status=_LocalAuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="client_id and client_secret are required",
            )

        if _HAS_SDK:
            await self.save_config({"client_id": client_id, "client_secret": client_secret})

        logger.info("box.install.ok", connector_id=self.connector_id)

        if _HAS_SDK:
            return ConnectorStatus(  # type: ignore[name-defined]
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                auth_status=AuthStatus.PENDING,  # type: ignore[name-defined]
                message="Connector installed — complete OAuth to connect",
            )
        return InstallResult(
            health=_LocalConnectorHealth.HEALTHY,
            auth_status=_LocalAuthStatus.PENDING,
            connector_id=self.connector_id,
            message="Connector installed — complete OAuth to connect",
        )

    # ── authorize ─────────────────────────────────────────────────────────────

    async def authorize(
        self,
        auth_code: Optional[str] = None,
        state: Optional[str] = None,
    ) -> Any:
        """Return Box OAuth2 authorization URL, or exchange auth_code for tokens.

        When called with no auth_code, returns the authorization URL string.
        When called with auth_code (SDK only), exchanges the code for tokens.
        """
        client_id = self.config.get("client_id", "")
        redirect_uri = self.config.get("redirect_uri", "")

        if auth_code is None:
            # Return the authorization URL
            params: Dict[str, str] = {
                "response_type": "code",
                "client_id": client_id,
            }
            if redirect_uri:
                params["redirect_uri"] = redirect_uri
            if state:
                params["state"] = state
            return f"{_AUTH_URI}?{urlencode(params)}"

        if not _HAS_SDK:
            raise NotImplementedError("authorize with auth_code requires the Shielva SDK")

        client_secret = self.config.get("client_secret", "")

        data = await self._ensure_client().post_form_data(
            url=_TOKEN_URI,
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
        scopes = (
            data.get("scope", "").split()
            if data.get("scope")
            else list(self.REQUIRED_SCOPES)
        )
        token_info = TokenInfo(  # type: ignore[name-defined]
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )
        await self.set_token(token_info)
        logger.info("box.authorize.ok", connector_id=self.connector_id)
        return token_info

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        """Check Box API connectivity by fetching the current user.

        Returns ConnectorStatus (SDK) or HealthCheckResult (standalone).
        """
        try:
            access_token = await self._get_valid_token()
            user = await with_retry(
                lambda: self._ensure_client().get_current_user(access_token),
                max_retries=2,
            )
            user_name: str = user.get("name", "")
            user_login: str = user.get("login", "")
            msg = f"Connected as {user_name} ({user_login})" if user_name else "Box API reachable"

            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                    auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                    message=msg,
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.HEALTHY,
                auth_status=_LocalAuthStatus.CONNECTED,
                message=msg,
                user_name=user_name,
                user_login=user_login,
            )
        except BoxAuthError:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.TOKEN_EXPIRED,  # type: ignore[name-defined]
                    message="Token expired — re-authorize the connector",
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.TOKEN_EXPIRED,
                message="Token expired — re-authorize the connector",
            )
        except BoxError as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.FAILED,
                message=str(exc),
            )

    # ── sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: Optional[datetime] = None,
        kb_id: str = "",
        webhook_url: Optional[str] = None,
    ) -> Any:
        """Sync all files recursively from root folder into the knowledge base.

        Traverses the Box folder tree breadth-first from root (folder_id="0").
        Normalizes each file into a ConnectorDocument and ingests it.
        Returns SyncResult.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            access_token = await self._get_valid_token()
            # BFS traversal: start from root
            folder_queue: List[str] = ["0"]
            visited_folders: set = set()

            while folder_queue:
                folder_id = folder_queue.pop(0)
                if folder_id in visited_folders:
                    continue
                visited_folders.add(folder_id)

                offset = 0
                limit = 100

                while True:
                    resp = await with_retry(
                        lambda fid=folder_id, off=offset: self._ensure_client().get_folder_items(
                            access_token,
                            folder_id=fid,
                            limit=limit,
                            offset=off,
                        ),
                        max_retries=3,
                    )

                    entries: List[Dict[str, Any]] = resp.get("entries", [])
                    total_count: int = resp.get("total_count", 0)

                    for entry in entries:
                        entry_type: str = entry.get("type", "")
                        entry_id: str = entry.get("id", "")

                        if entry_type == "folder":
                            folder_queue.append(entry_id)
                        elif entry_type == "file":
                            documents_found += 1
                            try:
                                doc = normalize_file(
                                    entry, self.connector_id, self.tenant_id
                                )
                                if _HAS_SDK:
                                    normalized = NormalizedDocument(  # type: ignore[name-defined]
                                        id=doc.id,
                                        source_id=entry_id,
                                        title=doc.title,
                                        content=doc.content,
                                        content_type="text",
                                        source_url=doc.metadata.get("shared_url", ""),
                                        author=doc.metadata.get("owned_by", ""),
                                        source="box",
                                        tenant_id=self.tenant_id,
                                        connector_id=self.connector_id,
                                        metadata=doc.metadata,
                                    )
                                    await self.ingest_document(
                                        normalized,
                                        kb_id=kb_id or "",
                                        webhook_url=webhook_url,
                                    )
                                documents_synced += 1
                            except Exception as exc:
                                logger.error(
                                    "box.sync.file_failed",
                                    file_id=entry_id,
                                    error=str(exc),
                                )
                                documents_failed += 1

                    # Box pagination: if we've received all items, stop
                    offset += len(entries)
                    if offset >= total_count or not entries:
                        break

            status = (
                _LocalSyncStatus.COMPLETED
                if documents_failed == 0
                else _LocalSyncStatus.PARTIAL
            )
            msg = f"Synced {documents_synced}/{documents_found} files"

            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=msg,
                )
            return _LocalSyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=msg,
            )

        except Exception as exc:
            logger.error(
                "box.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.FAILED,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=str(exc),
                )
            return _LocalSyncResult(
                status=_LocalSyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public convenience methods ────────────────────────────────────────────

    async def list_folder(
        self,
        folder_id: str = "0",
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List items in a Box folder.

        Returns the raw API response: {total_count, entries, offset, limit}.
        """
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self._ensure_client().get_folder_items(
                access_token,
                folder_id=folder_id,
                limit=limit,
                offset=offset,
            ),
            max_retries=3,
        )

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        """Fetch file metadata by file_id.

        Returns the raw Box API file object.
        """
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self._ensure_client().get_file(access_token, file_id),
            max_retries=3,
        )

    async def get_folder(self, folder_id: str) -> Dict[str, Any]:
        """Fetch folder metadata by folder_id.

        Returns the raw Box API folder object.
        """
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self._ensure_client().get_folder(access_token, folder_id),
            max_retries=3,
        )

    async def search(
        self,
        query: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Search Box for files and folders matching *query*.

        Returns the raw API response: {total_count, entries, offset, limit}.
        """
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self._ensure_client().search(
                access_token,
                query=query,
                limit=limit,
                offset=offset,
            ),
            max_retries=3,
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release any held async resources."""
        self._http_client = None

    async def __aenter__(self) -> "BoxConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
