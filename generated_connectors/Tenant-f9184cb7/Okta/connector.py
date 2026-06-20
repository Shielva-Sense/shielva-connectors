from __future__ import annotations

from typing import Any

from shared.base_connector import BaseConnector

from client import OktaHTTPClient
from exceptions import OktaAuthError, OktaError, OktaNetworkError
from helpers import (
    normalize_app,
    normalize_group,
    normalize_log,
    normalize_user,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

CONNECTOR_TYPE = "okta"
AUTH_TYPE = "api_key"

SYNC_USER_LIMIT = 200
SYNC_GROUP_LIMIT = 200
SYNC_APP_LIMIT = 200
SYNC_LOG_LIMIT = 100


class OktaConnector(BaseConnector):
    """
    Shielva connector for Okta Identity and Access Management.

    Provides authentication, health checks, full sync, and direct access to
    Okta users, groups, applications, system logs, and factors via the Okta
    REST API v1.

    Auth: SSWS API Token — Authorization: SSWS {api_token}
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._api_token: str = _config.get("api_token", "")
        self._domain: str = _config.get("domain", "")
        self.client: OktaHTTPClient | None = None

    def _make_client(self) -> OktaHTTPClient:
        return OktaHTTPClient(config=self.config)

    def _ensure_client(self) -> OktaHTTPClient:
        if self.client is None:
            self.client = self._make_client()
        return self.client

    def _has_credentials(self) -> bool:
        return bool(self._api_token and self._domain)

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials — api_token and domain must both be present."""
        if not self._api_token:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )
        if not self._domain:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="domain is required (e.g. dev-123456.okta.com)",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_me)
            await client.aclose()
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Okta",
            )
        except OktaAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Okta authentication failed: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /users/me and return current health status."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token and domain are required",
            )
        client = self._make_client()
        try:
            me = await with_retry(client.get_me)
            await client.aclose()
            profile: dict[str, Any] = me.get("profile", {}) or {}
            login: str = profile.get("login", "") or me.get("login", "") or ""
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Okta API is reachable. Authenticated as {login}",
                username=login,
            )
        except OktaAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except OktaNetworkError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(self, kb_id: str = "", **kwargs: Any) -> SyncResult:
        """Full sync of Okta users, groups, applications, and system logs."""
        if not self._has_credentials():
            return SyncResult(
                status=SyncStatus.FAILED,
                message="api_token and domain are required",
            )

        _ = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        # 1. Users
        try:
            users = await self.list_users()
            found += len(users)
            for raw in users:
                try:
                    doc = normalize_user(raw, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except OktaError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to fetch users: {exc}",
            )

        # 2. Groups
        try:
            groups = await self.list_groups()
            found += len(groups)
            for raw in groups:
                try:
                    doc = normalize_group(raw, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except OktaError:
            failed += 1

        # 3. Apps
        try:
            apps = await self.list_apps()
            found += len(apps)
            for raw in apps:
                try:
                    doc = normalize_app(raw, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except OktaError:
            failed += 1

        # 4. Logs
        try:
            logs = await self.list_logs()
            found += len(logs)
            for raw in logs:
                try:
                    doc = normalize_log(raw, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except OktaError:
            failed += 1

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch all users via cursor pagination (GET /users)."""
        http = self._ensure_client()
        all_users: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page, cursor = await with_retry(
                http.get_users, limit=SYNC_USER_LIMIT, after=cursor, **kwargs
            )
            all_users.extend(page)
            if not cursor:
                break
        return all_users

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """GET /users/{user_id}."""
        http = self._ensure_client()
        return await with_retry(http.get_user, user_id)

    # ── Groups ────────────────────────────────────────────────────────────────

    async def list_groups(self) -> list[dict[str, Any]]:
        """Fetch all groups via cursor pagination (GET /groups)."""
        http = self._ensure_client()
        all_groups: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page, cursor = await with_retry(
                http.get_groups, limit=SYNC_GROUP_LIMIT, after=cursor
            )
            all_groups.extend(page)
            if not cursor:
                break
        return all_groups

    # ── Apps ──────────────────────────────────────────────────────────────────

    async def list_apps(self) -> list[dict[str, Any]]:
        """Fetch all applications via cursor pagination (GET /apps)."""
        http = self._ensure_client()
        all_apps: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page, cursor = await with_retry(
                http.get_apps, limit=SYNC_APP_LIMIT, after=cursor
            )
            all_apps.extend(page)
            if not cursor:
                break
        return all_apps

    # ── Logs ──────────────────────────────────────────────────────────────────

    async def list_logs(self, since: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch system log events via cursor pagination (GET /logs)."""
        http = self._ensure_client()
        all_logs: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page, cursor = await with_retry(
                http.get_logs, limit=SYNC_LOG_LIMIT, after=cursor, since=since
            )
            all_logs.extend(page)
            if not cursor:
                break
        return all_logs

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def __aenter__(self) -> OktaConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
