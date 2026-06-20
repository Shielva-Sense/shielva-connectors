from __future__ import annotations

import asyncio
import urllib.parse
from datetime import datetime
from typing import Any, Dict

from client import DropboxHTTPClient
from exceptions import DropboxAuthError, DropboxError, DropboxNetworkError
from helpers import CircuitBreaker, normalize_file_metadata, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

from shared.base_connector import BaseConnector

# Dropbox OAuth2 endpoints
DROPBOX_AUTH_URL = "https://www.dropbox.com/oauth2/authorize"
DROPBOX_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"

CIRCUIT_BREAKER_THRESHOLD = 5
SYNC_FOLDER_LIMIT = 200  # entries per list_folder page


class DropboxConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Dropbox.

    Provides OAuth2 authorization URL generation, authentication validation,
    health checks, recursive file listing, metadata retrieval, and full sync
    of all Dropbox files into the knowledge base.
    """

    CONNECTOR_TYPE: str = "dropbox"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(
                tenant_id=tenant_id, connector_id=connector_id, config=_config
            )
        else:
            self.config = _config
            self.connector_id = connector_id
            self.tenant_id = tenant_id

        # Support both spec field names (client_id/client_secret) and legacy (app_key/app_secret)
        self._app_key: str = _config.get("client_id", _config.get("app_key", ""))
        self._app_secret: str = _config.get("client_secret", _config.get("app_secret", ""))
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")
        self._account_id: str = _config.get("account_id", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self.http_client: DropboxHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=CIRCUIT_BREAKER_THRESHOLD
        )

    def _make_client(self) -> DropboxHTTPClient:
        return DropboxHTTPClient(access_token=self._access_token)

    def _has_credentials(self) -> bool:
        """True when we have enough to make API calls."""
        return bool(self._access_token)

    def _has_app_credentials(self) -> bool:
        """True when app_key and app_secret are both present."""
        return bool(self._app_key and self._app_secret)

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate app_key/app_secret (and access_token if provided).

        If access_token is present, calls /users/get_current_account to confirm
        the token works.  If only app_key/app_secret are supplied (no token yet),
        confirms they are non-empty and returns CONNECTED so the caller can
        proceed to authorize().
        """
        if not self._has_app_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="app_key and app_secret are required",
            )

        # If we already have an access_token, validate it live
        if self._access_token:
            client = self._make_client()
            try:
                await with_retry(client.get_current_account)
                await client.aclose()
                self.http_client = self._make_client()
                return InstallResult(
                    health=ConnectorHealth.HEALTHY,
                    auth_status=AuthStatus.CONNECTED,
                    connector_id=self.connector_id,
                    message="Connected to Dropbox",
                )
            except DropboxAuthError as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Dropbox authentication failed: {exc}",
                )
            except Exception as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.FAILED,
                    message=str(exc),
                )

        # app_key + app_secret present but no token yet — install succeeds;
        # user must call authorize() next.
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message=(
                "Dropbox app credentials validated. "
                "Call authorize() to obtain an access token."
            ),
        )

    def authorize(self) -> str:
        """Return the Dropbox OAuth2 authorization URL.

        The user should be redirected to this URL to grant access.
        After approval, Dropbox redirects to redirect_uri with a `code`
        parameter that must be exchanged for an access token at
        DROPBOX_TOKEN_URL.
        """
        params: dict[str, str] = {
            "client_id": self._app_key,
            "response_type": "code",
            "token_access_type": "offline",
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        return f"{DROPBOX_AUTH_URL}?{urllib.parse.urlencode(params)}"

    # ── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """POST /users/get_current_account + /users/get_space_usage to verify token."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required",
            )
        client = self._make_client()
        try:
            account, _space = await asyncio.gather(
                with_retry(client.get_current_account),
                with_retry(client.get_space_usage),
            )
            await client.aclose()
            self._circuit_breaker.on_success()
            display_name = account.get("name", {}).get("display_name", "")
            email = account.get("email", "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Dropbox API is reachable",
                display_name=display_name,
                email=email,
            )
        except DropboxAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except DropboxNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = (
                ConnectorHealth.DEGRADED
                if not self._circuit_breaker.is_open
                else ConnectorHealth.OFFLINE
            )
            return HealthCheckResult(
                health=health,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """Sync all Dropbox files recursively into the knowledge base.

        Uses list_folder with recursive=True from root ("") to enumerate
        all files and folders, following cursor pagination via list_folder_continue.
        """
        _ = since  # Dropbox list_folder has no server-side since filter
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        try:
            entries = await self._fetch_all_entries()
        except DropboxError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        found = len(entries)
        for entry in entries:
            try:
                doc = normalize_file_metadata(
                    entry, self.connector_id, self.tenant_id
                )
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_entries(self) -> list[dict[str, Any]]:
        """Fetch all file/folder entries recursively using cursor pagination."""
        assert self.http_client is not None
        entries: list[dict[str, Any]] = []

        page = await with_retry(
            self.http_client.list_folder,
            path="",
            recursive=True,
            limit=SYNC_FOLDER_LIMIT,
        )
        entries.extend(page.get("entries", []))

        while page.get("has_more"):
            cursor = page["cursor"]
            page = await with_retry(
                self.http_client.list_folder_continue, cursor
            )
            entries.extend(page.get("entries", []))

        return entries

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Direct API operations ─────────────────────────────────────────────────

    async def list_folder(
        self, path: str = "", recursive: bool = False
    ) -> dict[str, Any]:
        """POST /files/list_folder."""
        client = self._ensure_client()
        return await with_retry(client.list_folder, path=path, recursive=recursive)

    async def list_folder_continue(self, cursor: str) -> dict[str, Any]:
        """POST /files/list_folder/continue."""
        client = self._ensure_client()
        return await with_retry(client.list_folder_continue, cursor)

    async def get_metadata(self, path: str) -> dict[str, Any]:
        """POST /files/get_metadata."""
        client = self._ensure_client()
        return await with_retry(client.get_metadata, path)

    async def search_files(
        self, query: str, max_results: int = 100
    ) -> dict[str, Any]:
        """POST /files/search_v2."""
        client = self._ensure_client()
        return await with_retry(client.search_files, query, max_results=max_results)

    async def list_shared_links(self, path: str | None = None) -> dict[str, Any]:
        """POST /sharing/list_shared_links."""
        client = self._ensure_client()
        return await with_retry(client.list_shared_links, path=path)

    async def get_space_usage(self) -> dict[str, Any]:
        """POST /users/get_space_usage."""
        client = self._ensure_client()
        return await with_retry(client.get_space_usage)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_client(self) -> DropboxHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> DropboxConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
