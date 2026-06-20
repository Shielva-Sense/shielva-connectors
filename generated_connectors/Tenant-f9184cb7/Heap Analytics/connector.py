from __future__ import annotations

from typing import Any

from client.http_client import HeapHTTPClient
from exceptions import HeapAuthError, HeapError, HeapNetworkError
from helpers.utils import (
    normalize_event,
    normalize_segment,
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
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}


CONNECTOR_TYPE = "heap"
AUTH_TYPE = "api_key"


class HeapConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Heap Analytics.

    Provides authentication, health checks, full sync, and direct API access
    for events, sessions, users, funnels, segments, and user properties
    via the Heap REST and Server-Side APIs.

    Auth: Bearer token. Header — Authorization: Bearer {api_key}
    The account_id corresponds to the Heap App ID.
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
        if BaseConnector is not object:
            try:
                super().__init__(
                    tenant_id=tenant_id,
                    connector_id=connector_id,
                    config=_config,
                )
            except TypeError:
                self.tenant_id = tenant_id
                self.connector_id = connector_id
                self.config = _config
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = _config

        self._api_key: str = _config.get("api_key", "")
        self._account_id: str = _config.get("account_id", "")
        self.client: HeapHTTPClient | None = None

    def _make_client(self) -> HeapHTTPClient:
        return HeapHTTPClient(config=self.config)

    def _ensure_client(self) -> HeapHTTPClient:
        if self.client is None:
            self.client = self._make_client()
        return self.client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key and account_id via POST /api/track (health ping)."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        if not self._account_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="account_id (Heap App ID) is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.validate_credentials)
            await client.aclose()
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Heap Analytics (App ID: {self._account_id})",
            )
        except HeapAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Heap credentials: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping POST /api/track and return current health."""
        if not self._api_key or not self._account_id:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key and account_id are required",
            )
        client = self._make_client()
        try:
            await with_retry(client.validate_credentials)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Heap Analytics (App ID: {self._account_id})",
            )
        except HeapAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except HeapNetworkError as exc:
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

    async def sync(
        self,
        full: bool = False,  # noqa: ARG002
        since: Any = None,  # noqa: ARG002
        kb_id: str = "",
    ) -> SyncResult:
        """Sync users, events, and segments from Heap Analytics."""
        if self.client is None:
            self.client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync users
        try:
            users_resp = await with_retry(self.client.get_users)
            users: list[dict[str, Any]] = users_resp.get(
                "users", users_resp.get("data", [])
            )
            found += len(users)
            for raw_user in users:
                try:
                    doc = normalize_user(
                        raw_user,
                        self._account_id,
                        self.connector_id,
                        self.tenant_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except HeapError:
            pass

        # Sync events
        try:
            events_resp = await with_retry(self.client.get_events)
            events: list[dict[str, Any]] = events_resp.get(
                "events", events_resp.get("data", [])
            )
            found += len(events)
            for raw_event in events:
                try:
                    doc = normalize_event(
                        raw_event,
                        self._account_id,
                        self.connector_id,
                        self.tenant_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except HeapError:
            pass

        # Sync segments
        try:
            segments_resp = await with_retry(self.client.get_segments)
            segments: list[dict[str, Any]] = segments_resp.get(
                "segments", segments_resp.get("data", [])
            )
            found += len(segments)
            for raw_segment in segments:
                try:
                    doc = normalize_segment(
                        raw_segment,
                        self.connector_id,
                        self.tenant_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except HeapError:
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        if found == 0 and synced == 0:
            status = SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(self, page: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """List Heap users (paginated)."""
        client = self._ensure_client()
        resp = await with_retry(client.get_users, page=page, limit=limit)
        return resp.get("users", resp.get("data", []))

    # ── Events ─────────────────────────────────────────────────────────────────

    async def list_events(
        self,
        event_name: str | None = None,
        time_range_days: int = 30,
    ) -> list[dict[str, Any]]:
        """List Heap events (aggregated counts)."""
        client = self._ensure_client()
        resp = await with_retry(
            client.get_events,
            event_name=event_name,
            time_range_days=time_range_days,
        )
        return resp.get("events", resp.get("data", []))

    # ── Segments ──────────────────────────────────────────────────────────────

    async def list_segments(self) -> list[dict[str, Any]]:
        """List all Heap segments."""
        client = self._ensure_client()
        resp = await with_retry(client.get_segments)
        return resp.get("segments", resp.get("data", []))

    # ── Server-side tracking ──────────────────────────────────────────────────

    async def track_event(
        self,
        identity: str,
        event: str,
        properties: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Server-side event tracking via POST /api/track."""
        client = self._ensure_client()
        return await with_retry(
            client.track_event, identity, event, properties
        )

    async def identify_user(
        self, identity: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        """Server-side user identification via POST /api/identify."""
        client = self._ensure_client()
        return await with_retry(client.identify_user, identity, properties)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def __aenter__(self) -> HeapConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
