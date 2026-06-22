from __future__ import annotations

from typing import Any

from client.http_client import FullStoryHTTPClient
from exceptions import FullStoryAuthError, FullStoryError, FullStoryNetworkError
from helpers.utils import (
    normalize_segment,
    normalize_session,
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

from shared.base_connector import BaseConnector


CONNECTOR_TYPE = "fullstory"
AUTH_TYPE = "api_key"


class FullStoryConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for FullStory digital experience analytics.

    Provides authentication, health checks, full sync, and direct API access
    for session recordings, users, segments, and custom events via the
    FullStory REST API v2.

    Auth: Bearer token. Header — Authorization: Bearer {api_key}
    Base URL: https://api.fullstory.com
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
        self.client: FullStoryHTTPClient | None = None

    def _make_client(self) -> FullStoryHTTPClient:
        return FullStoryHTTPClient(config=self.config)

    def _ensure_client(self) -> FullStoryHTTPClient:
        if self.client is None:
            self.client = self._make_client()
        return self.client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key by calling GET /v2/org."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            org = await with_retry(client.get_org)
            await client.aclose()
            org_name: str = str(org.get("displayName", org.get("name", "FullStory")))
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to FullStory organization: {org_name}",
            )
        except FullStoryAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid FullStory API key: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /v2/org and return current health."""
        if not self._api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            org = await with_retry(client.get_org)
            await client.aclose()
            org_name: str = str(org.get("displayName", org.get("name", "FullStory")))
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to FullStory organization: {org_name}",
            )
        except FullStoryAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except FullStoryNetworkError as exc:
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
        """Sync users and segments from FullStory.

        Sessions are per-user and too heavy for a full sync; use
        list_sessions(uid=...) for targeted retrieval.
        """
        if self.client is None:
            self.client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync users
        try:
            users_resp = await with_retry(self.client.get_users)
            users: list[dict[str, Any]] = users_resp.get(
                "users", users_resp.get("data", users_resp.get("results", []))
            )
            found += len(users)
            for raw_user in users:
                try:
                    doc = normalize_user(
                        raw_user,
                        self.connector_id,
                        self.tenant_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except FullStoryError:
            pass

        # Sync segments
        try:
            segments_resp = await with_retry(self.client.get_segments)
            segments: list[dict[str, Any]] = segments_resp.get(
                "segments", segments_resp.get("data", segments_resp.get("results", []))
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
        except FullStoryError:
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

    async def list_users(
        self,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """List FullStory users (paginated)."""
        client = self._ensure_client()
        resp = await with_retry(client.get_users, limit=limit, cursor=cursor)
        return resp.get("users", resp.get("data", resp.get("results", [])))

    async def get_user(self, uid: str) -> dict[str, Any]:
        """Retrieve a single FullStory user by UID."""
        client = self._ensure_client()
        return await with_retry(client.get_user, uid)

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def list_sessions(
        self,
        uid: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """List FullStory session recordings (optionally filtered by user UID)."""
        client = self._ensure_client()
        resp = await with_retry(client.get_sessions, uid=uid, limit=limit, cursor=cursor)
        return resp.get("sessions", resp.get("data", resp.get("results", [])))

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Retrieve a single FullStory session recording by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_session, session_id)

    # ── Segments ──────────────────────────────────────────────────────────────

    async def list_segments(self, limit: int = 100) -> list[dict[str, Any]]:
        """List all FullStory user segments."""
        client = self._ensure_client()
        resp = await with_retry(client.get_segments, limit=limit)
        return resp.get("segments", resp.get("data", resp.get("results", [])))

    # ── Events ────────────────────────────────────────────────────────────────

    async def list_events(
        self,
        uid: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List custom events for a specific FullStory user."""
        client = self._ensure_client()
        resp = await with_retry(client.get_events, uid, limit=limit)
        return resp.get("events", resp.get("data", resp.get("results", [])))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def __aenter__(self) -> FullStoryConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
