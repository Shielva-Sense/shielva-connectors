"""Auth0 connector — machine-to-machine client credentials via Auth0 Management API v2."""

from __future__ import annotations

from typing import Any

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

from client import Auth0HTTPClient
from exceptions import Auth0AuthError, Auth0Error, Auth0NetworkError
from helpers import (
    normalize_client,
    normalize_connection,
    normalize_log,
    normalize_role,
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

CONNECTOR_TYPE = "auth0"
AUTH_TYPE = "oauth2"

SYNC_USER_LIMIT = 100
SYNC_ROLE_LIMIT = 100
SYNC_CLIENT_LIMIT = 100
SYNC_CONNECTION_LIMIT = 100
SYNC_LOG_LIMIT = 100


class Auth0Connector(BaseConnector):
    """
    Shielva connector for Auth0 (Okta) Identity Management.

    Provides authentication, health checks, full sync, and direct access to
    Auth0 users, roles, clients (applications), connections, and logs via the
    Auth0 Management API v2.

    Auth: OAuth 2.0 Machine-to-Machine (Client Credentials)
        POST https://{domain}/oauth/token
        grant_type=client_credentials, audience=https://{domain}/api/v2/
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
        self._domain: str = _config.get("domain", "").strip().rstrip("/")
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self.client: Auth0HTTPClient | None = None

    def _make_client(self) -> Auth0HTTPClient:
        return Auth0HTTPClient(config=self.config)

    def _ensure_client(self) -> Auth0HTTPClient:
        if self.client is None:
            self.client = self._make_client()
        return self.client

    def _has_credentials(self) -> bool:
        return bool(self._domain and self._client_id and self._client_secret)

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials — domain, client_id, client_secret must all be present."""
        if not self._domain:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="domain is required (e.g. myapp.auth0.com)",
            )
        if not self._client_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id is required",
            )
        if not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_secret is required",
            )

        client = self._make_client()
        try:
            await with_retry(client.authenticate)
            # Verify the token works against a real endpoint
            await with_retry(client.get_users, page=0, per_page=1, include_totals=False)
            await client.aclose()
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Auth0 tenant {self._domain}",
            )
        except Auth0AuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Auth0 authentication failed: {exc}",
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
        """Probe GET /api/v2/users?per_page=1 and return current health status."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="domain, client_id, and client_secret are all required",
            )
        client = self._make_client()
        try:
            result = await with_retry(client.get_users, page=0, per_page=1, include_totals=True)
            await client.aclose()
            total: int = result.get("total", 0) if isinstance(result, dict) else 0
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Auth0 Management API is reachable. Tenant: {self._domain} ({total} users total)",
                username=self._domain,
            )
        except Auth0AuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Auth0NetworkError as exc:
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
        """Full sync of Auth0 users, roles, clients, connections, and logs."""
        if not self._has_credentials():
            return SyncResult(
                status=SyncStatus.FAILED,
                message="domain, client_id, and client_secret are all required",
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
        except Auth0Error as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to fetch users: {exc}",
            )

        # 2. Roles
        try:
            roles = await self.list_roles()
            found += len(roles)
            for raw in roles:
                try:
                    doc = normalize_role(raw, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Auth0Error:
            failed += 1

        # 3. Clients (Applications)
        try:
            clients = await self.list_clients()
            found += len(clients)
            for raw in clients:
                try:
                    doc = normalize_client(raw, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Auth0Error:
            failed += 1

        # 4. Connections
        try:
            connections = await self.list_connections()
            found += len(connections)
            for raw in connections:
                try:
                    doc = normalize_connection(raw, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Auth0Error:
            failed += 1

        # 5. Logs
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
        except Auth0Error:
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
        """Fetch all users via page-based pagination (GET /api/v2/users)."""
        http = self._ensure_client()
        all_users: list[dict[str, Any]] = []
        page = 0
        while True:
            result = await with_retry(
                http.get_users,
                page=page,
                per_page=SYNC_USER_LIMIT,
                include_totals=True,
                **kwargs,
            )
            page_users: list[dict[str, Any]] = result.get("users", []) if isinstance(result, dict) else []
            all_users.extend(page_users)
            if len(page_users) < SYNC_USER_LIMIT:
                break
            page += 1
        return all_users

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """GET /api/v2/users/{id}."""
        http = self._ensure_client()
        return await with_retry(http.get_user, user_id)

    # ── Roles ─────────────────────────────────────────────────────────────────

    async def list_roles(self) -> list[dict[str, Any]]:
        """Fetch all roles via page-based pagination (GET /api/v2/roles)."""
        http = self._ensure_client()
        all_roles: list[dict[str, Any]] = []
        page = 0
        while True:
            result = await with_retry(http.get_roles, page=page, per_page=SYNC_ROLE_LIMIT)
            page_roles: list[dict[str, Any]] = result.get("roles", []) if isinstance(result, dict) else []
            all_roles.extend(page_roles)
            if len(page_roles) < SYNC_ROLE_LIMIT:
                break
            page += 1
        return all_roles

    # ── Clients (Applications) ─────────────────────────────────────────────────

    async def list_clients(self, app_type: str | None = None) -> list[dict[str, Any]]:
        """Fetch all clients (applications) via page-based pagination (GET /api/v2/clients)."""
        http = self._ensure_client()
        all_clients: list[dict[str, Any]] = []
        page = 0
        while True:
            result = await with_retry(
                http.get_clients,
                page=page,
                per_page=SYNC_CLIENT_LIMIT,
                app_type=app_type,
            )
            page_clients: list[dict[str, Any]] = result.get("clients", []) if isinstance(result, dict) else []
            all_clients.extend(page_clients)
            if len(page_clients) < SYNC_CLIENT_LIMIT:
                break
            page += 1
        return all_clients

    # ── Connections ───────────────────────────────────────────────────────────

    async def list_connections(self) -> list[dict[str, Any]]:
        """Fetch all connections via page-based pagination (GET /api/v2/connections)."""
        http = self._ensure_client()
        all_connections: list[dict[str, Any]] = []
        page = 0
        while True:
            result = await with_retry(
                http.get_connections,
                page=page,
                per_page=SYNC_CONNECTION_LIMIT,
            )
            page_connections: list[dict[str, Any]] = result.get("connections", []) if isinstance(result, dict) else []
            all_connections.extend(page_connections)
            if len(page_connections) < SYNC_CONNECTION_LIMIT:
                break
            page += 1
        return all_connections

    # ── Logs ──────────────────────────────────────────────────────────────────

    async def list_logs(self, from_: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch log events via page-based or cursor-based pagination (GET /api/v2/logs)."""
        http = self._ensure_client()
        all_logs: list[dict[str, Any]] = []
        page = 0
        while True:
            page_logs = await with_retry(
                http.get_logs,
                page=page,
                per_page=SYNC_LOG_LIMIT,
                from_=from_,
            )
            if not isinstance(page_logs, list):
                break
            all_logs.extend(page_logs)
            if len(page_logs) < SYNC_LOG_LIMIT:
                break
            if from_:
                # cursor-based: only one page when from_ is set
                break
            page += 1
        return all_logs

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def __aenter__(self) -> Auth0Connector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
