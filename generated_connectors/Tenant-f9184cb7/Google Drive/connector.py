"""Google Drive connector — orchestration only.

All HTTP calls  → client/http_client.py
All normalization + retry → helpers/utils.py
All models (standalone) → models.py

Imports BaseConnector via a try/except guard so this module loads cleanly
in the gateway's AST sandbox even when the Shielva SDK is absent.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from shared.base_connector import BaseConnector

from client.http_client import GoogleDriveHTTPClient
from exceptions import (
    GoogleDriveAuthError,
    GoogleDriveError,
)
from helpers.utils import normalize_file, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_DRIVE_BASE = "https://www.googleapis.com/drive/v3"

CONNECTOR_TYPE = "google_drive"


class GoogleDriveConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for the Google Drive API v3."""

    CONNECTOR_TYPE = "google_drive"
    CONNECTOR_NAME = "Google Drive"
    AUTH_TYPE = "oauth2"
    AUTH_URI = _AUTH_URI
    TOKEN_URI = _TOKEN_URI

    REQUIRED_SCOPES: List[str] = [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
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
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)

        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")

        base = _config.get("base_url", _DRIVE_BASE)
        self.http_client = GoogleDriveHTTPClient(base_url=base)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _has_credentials(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _get_access_token(self) -> str:
        """Return the current access token."""
        return self._access_token

    # ── Auth ────────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install-time config (client_id + client_secret) and return status."""
        client_id = self.config.get("client_id", self._client_id)
        client_secret = self.config.get("client_secret", self._client_secret)

        if not client_id or not client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="client_id and client_secret are required",
            )

        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Connector installed — complete OAuth to connect",
        )

    async def authorize(self) -> str:
        """Return the Google OAuth2 authorization URL with drive scopes.

        Includes access_type=offline so Google issues a refresh token.
        The caller (platform gateway) redirects the user's browser to this URL.
        """
        client_id = self.config.get("client_id", self._client_id)
        redirect_uri = self.config.get("redirect_uri", self._redirect_uri)
        params: Dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.REQUIRED_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{_AUTH_URI}?{urlencode(params)}"

    async def exchange_code(self, auth_code: str) -> Dict[str, Any]:
        """Exchange an OAuth2 authorization code for access + refresh tokens."""
        client_id = self.config.get("client_id", self._client_id)
        client_secret = self.config.get("client_secret", self._client_secret)
        redirect_uri = self.config.get("redirect_uri", self._redirect_uri)

        token_data = await self.http_client.exchange_code_for_token(
            client_id=client_id,
            client_secret=client_secret,
            code=auth_code,
            redirect_uri=redirect_uri,
        )
        self._access_token = token_data.get("access_token", "")
        self._refresh_token = token_data.get("refresh_token", "")
        return token_data

    async def _do_refresh_token(self) -> str:
        """Refresh the access token using the stored refresh token."""
        client_id = self.config.get("client_id", self._client_id)
        client_secret = self.config.get("client_secret", self._client_secret)
        refresh_token = self._refresh_token or self.config.get("refresh_token", "")
        if not refresh_token:
            raise GoogleDriveAuthError("No refresh token available — re-authorize the connector")

        data = await self.http_client.refresh_access_token(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
        new_token = data.get("access_token", "")
        self._access_token = new_token
        return new_token

    # ── Health ──────────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Check Google Drive API connectivity via GET /about?fields=user,storageQuota."""
        access_token = self._get_access_token()
        if not access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="No access token — complete OAuth authorization first",
            )
        try:
            data = await with_retry(
                self.http_client.get_about, access_token, max_retries=2
            )
            user_email: str = data.get("user", {}).get("emailAddress", "")
            quota: Dict[str, Any] = data.get("storageQuota", {})
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Google Drive API reachable",
                user_email=user_email,
                storage_quota=quota,
            )
        except GoogleDriveAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Token expired — re-authorize the connector: {exc}",
            )
        except GoogleDriveError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync all Drive files into the Shielva knowledge base.

        Pages through /files via nextPageToken until exhausted.
        """
        kb_id: str = kwargs.get("kb_id", "")
        access_token = self._get_access_token()
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            all_files = await self._collect_all_files(access_token)
            documents_found = len(all_files)

            for raw_file in all_files:
                try:
                    doc = normalize_file(raw_file, self.connector_id, self.tenant_id)
                    documents_synced += 1
                except Exception:
                    documents_failed += 1

            status = (
                SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} files",
            )
        except Exception as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    async def _collect_all_files(
        self, access_token: str
    ) -> List[Dict[str, Any]]:
        """Page through /files and collect all file metadata dicts."""
        files: List[Dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            resp = await with_retry(
                self.http_client.list_files,
                access_token,
                page_size=100,
                page_token=page_token,
                max_retries=3,
            )
            files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return files

    # ── File operations ─────────────────────────────────────────────────────

    async def list_files(
        self,
        page_size: int = 100,
        page_token: Optional[str] = None,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List Drive files with optional query filter.

        Returns the raw API response: {files: [...], nextPageToken}.
        """
        access_token = self._get_access_token()
        return await with_retry(
            self.http_client.list_files,
            access_token,
            page_size=page_size,
            query=query,
            page_token=page_token,
            max_retries=3,
        )

    async def list_folders(
        self,
        page_size: int = 100,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List Drive folders only.

        Returns the raw API response: {files: [...], nextPageToken}.
        """
        access_token = self._get_access_token()
        return await with_retry(
            self.http_client.list_folders,
            access_token,
            page_size=page_size,
            page_token=page_token,
            max_retries=3,
        )

    async def list_shared_drives(
        self,
        page_size: int = 100,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List shared drives accessible to the authenticated user.

        Returns the raw API response: {drives: [...], nextPageToken}.
        """
        access_token = self._get_access_token()
        return await with_retry(
            self.http_client.list_drives,
            access_token,
            page_size=page_size,
            page_token=page_token,
            max_retries=3,
        )

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        """Fetch full metadata for a single Drive file by ID."""
        access_token = self._get_access_token()
        return await with_retry(
            self.http_client.get_file,
            access_token,
            file_id,
            max_retries=3,
        )

    async def search_files(
        self, query: str, page_size: int = 100
    ) -> List[Dict[str, Any]]:
        """Search Drive files by query string. Returns a flat list of file dicts."""
        access_token = self._get_access_token()
        resp = await with_retry(
            self.http_client.search_files,
            access_token,
            query,
            page_size=page_size,
            max_retries=3,
        )
        return resp.get("files", [])

    async def get_permissions(self, file_id: str) -> Dict[str, Any]:
        """List permissions for a Drive file by ID."""
        access_token = self._get_access_token()
        return await with_retry(
            self.http_client.get_permissions,
            access_token,
            file_id,
            max_retries=3,
        )

    async def export_file(self, file_id: str, mime_type: str) -> bytes:
        """Export a Google Docs/Sheets/Slides file to a given MIME type."""
        access_token = self._get_access_token()
        return await with_retry(
            self.http_client.export_file,
            access_token,
            file_id,
            mime_type,
            max_retries=3,
        )
