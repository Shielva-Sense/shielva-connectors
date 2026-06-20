from __future__ import annotations

from typing import Any, Dict
from urllib.parse import urlencode

from client import DialpadHTTPClient
from exceptions import DialpadAuthError, DialpadError, DialpadNetworkError
from helpers import (
    CircuitBreaker,
    normalize_call_log,
    normalize_contact,
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

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: Dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

DIALPAD_OAUTH_AUTH_URL = "https://dialpad.com/oauth2/authorize"
DIALPAD_OAUTH_TOKEN_URL = "https://dialpad.com/oauth2/token"
DIALPAD_OAUTH_SCOPES = "calls contacts users"
CIRCUIT_BREAKER_THRESHOLD = 5


class DialpadConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Dialpad.

    Provides OAuth2 authentication, health checks, and full sync of call logs
    and contacts via the Dialpad REST API v2.
    """

    CONNECTOR_TYPE: str = "dialpad"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")
        self._token_expires_at: str = _config.get("token_expires_at", "")
        self.http_client: DialpadHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> DialpadHTTPClient:
        return DialpadHTTPClient(access_token=self._access_token)

    def _ensure_client(self) -> DialpadHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    def _has_credentials(self) -> bool:
        """True when we have enough to authenticate."""
        return bool(self._access_token or (self._client_id and self._client_secret))

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that client_id and client_secret are present."""
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
        # If we already have an access token, probe the API.
        if self._access_token:
            client = self._make_client()
            try:
                await with_retry(client.get_user)
                await client.aclose()
                self.http_client = self._make_client()
                return InstallResult(
                    health=ConnectorHealth.HEALTHY,
                    auth_status=AuthStatus.CONNECTED,
                    connector_id=self.connector_id,
                    message="Connected to Dialpad API",
                )
            except DialpadAuthError as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Dialpad authentication failed: {exc}",
                )
            except Exception as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.FAILED,
                    message=str(exc),
                )
        # No access token yet — OAuth flow must be completed.
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Dialpad OAuth credentials accepted. Complete authorization via the OAuth flow.",
        )

    def authorize(self) -> str:
        """Return the Dialpad OAuth2 authorization URL."""
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
            "scope": DIALPAD_OAUTH_SCOPES,
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        return f"{DIALPAD_OAUTH_AUTH_URL}?{urlencode(params)}"

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /api/v2/users/me and return current health."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret (or access_token) are required",
            )
        if not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="OAuth flow not completed — access_token is missing",
            )
        client = self._make_client()
        try:
            user = await with_retry(client.get_user)
            await client.aclose()
            self._circuit_breaker.on_success()
            name = user.get("display_name") or user.get("name") or user.get("email") or "unknown"
            email = user.get("email", "")
            msg = f"Connected as {name}"
            if email:
                msg = f"Connected as {name} ({email})"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
            )
        except DialpadAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except DialpadNetworkError as exc:
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

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync Dialpad call logs and contacts into the knowledge base."""
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync call logs
        try:
            call_logs = await self._fetch_all_call_logs()
        except DialpadError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        found += len(call_logs)
        for record in call_logs:
            try:
                doc = normalize_call_log(record, self.connector_id, self.tenant_id)
                kb_id = kwargs.get("kb_id", "")
                if kb_id:
                    await self._ingest_document(doc, str(kb_id))
                synced += 1
            except Exception:
                failed += 1

        # Sync contacts
        try:
            contacts = await self._fetch_all_contacts()
        except DialpadError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        found += len(contacts)
        for record in contacts:
            try:
                doc = normalize_contact(record, self.connector_id, self.tenant_id)
                kb_id = kwargs.get("kb_id", "")
                if kb_id:
                    await self._ingest_document(doc, str(kb_id))
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

    async def _fetch_all_call_logs(
        self, started_after: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                self.http_client.get_call_logs,
                cursor=cursor,
                started_after=started_after,
            )
            items = page.get("items", page.get("data", []))
            records.extend(items)
            cursor = page.get("cursor") or None
            if not cursor:
                break
        return records

    async def _fetch_all_contacts(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                self.http_client.get_contacts,
                cursor=cursor,
            )
            items = page.get("items", page.get("data", []))
            records.extend(items)
            cursor = page.get("cursor") or None
            if not cursor:
                break
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── List methods ─────────────────────────────────────────────────────────

    async def list_users(self) -> list[dict[str, Any]]:
        """Fetch all users via cursor pagination."""
        client = self._ensure_client()
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(client.get_users, cursor=cursor)
            items = page.get("items", page.get("data", []))
            records.extend(items)
            cursor = page.get("cursor") or None
            if not cursor:
                break
        return records

    async def list_call_logs(
        self, started_after: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch all call logs via cursor pagination, optionally filtered by started_after."""
        client = self._ensure_client()
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                client.get_call_logs,
                cursor=cursor,
                started_after=started_after,
            )
            items = page.get("items", page.get("data", []))
            records.extend(items)
            cursor = page.get("cursor") or None
            if not cursor:
                break
        return records

    async def list_contacts(self) -> list[dict[str, Any]]:
        """Fetch all contacts via cursor pagination."""
        client = self._ensure_client()
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(client.get_contacts, cursor=cursor)
            items = page.get("items", page.get("data", []))
            records.extend(items)
            cursor = page.get("cursor") or None
            if not cursor:
                break
        return records

    async def list_departments(self) -> list[dict[str, Any]]:
        """Fetch all departments via cursor pagination."""
        client = self._ensure_client()
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(client.get_departments, cursor=cursor)
            items = page.get("items", page.get("data", []))
            records.extend(items)
            cursor = page.get("cursor") or None
            if not cursor:
                break
        return records

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> DialpadConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
