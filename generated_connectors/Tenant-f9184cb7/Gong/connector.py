from __future__ import annotations

from typing import Any

from client import GongHTTPClient
from exceptions import GongAuthError, GongError, GongNetworkError
from helpers import normalize_call, normalize_transcript, normalize_user, with_retry
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


SYNC_PAGE_SIZE = 100


class GongConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Gong (revenue intelligence).

    Authenticates via HTTP Basic Auth (access_key:access_key_secret).
    Syncs calls, transcripts, and users via the Gong REST API v2.
    """

    CONNECTOR_TYPE: str = "gong"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._access_key: str = _config.get("access_key", "")
        self._access_key_secret: str = _config.get("access_key_secret", "")
        self.http_client: GongHTTPClient | None = None

    def _make_client(self) -> GongHTTPClient:
        return GongHTTPClient(
            access_key=self._access_key,
            access_key_secret=self._access_key_secret,
        )

    def _ensure_client(self) -> GongHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Install / health ──────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that access_key and access_key_secret are present."""
        if not self._access_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_key is required",
            )
        if not self._access_key_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_key_secret is required",
            )
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Gong credentials accepted",
        )

    async def health_check(self) -> HealthCheckResult:
        """Probe GET /v2/users (limit 1 cursor page) to verify connectivity."""
        if not (self._access_key and self._access_key_secret):
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_key and access_key_secret are required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_users)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Gong API is reachable",
            )
        except GongAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except GongNetworkError as exc:
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

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Fetch calls and users; normalize each into a ConnectorDocument.

        Accepts optional keyword args:
          from_date (str): ISO 8601 lower bound for call filter
          to_date   (str): ISO 8601 upper bound for call filter
          kb_id     (str): knowledge-base ID to push documents into
        """
        from_date: str | None = kwargs.get("from_date")
        to_date: str | None = kwargs.get("to_date")
        kb_id: str = kwargs.get("kb_id", "")

        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0

        # Sync calls
        try:
            calls = await self._fetch_all_calls(
                client, from_date=from_date, to_date=to_date
            )
        except GongError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        found += len(calls)
        for raw_call in calls:
            try:
                doc = normalize_call(raw_call)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # Sync users
        try:
            users = await self._fetch_all_users(client)
        except GongError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        found += len(users)
        for raw_user in users:
            try:
                doc = normalize_user(raw_user)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
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

    async def _fetch_all_calls(
        self,
        client: GongHTTPClient,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                client.get_calls,
                cursor=cursor,
                from_date=from_date,
                to_date=to_date,
            )
            calls = page.get("calls", []) or []
            records.extend(calls)
            cursor = page.get("records", {}).get("cursor") or None
            if not cursor:
                break
        return records

    async def _fetch_all_users(
        self, client: GongHTTPClient
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(client.get_users, cursor=cursor)
            users = page.get("users", []) or []
            records.extend(users)
            cursor = page.get("records", {}).get("cursor") or None
            if not cursor:
                break
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Public domain methods ─────────────────────────────────────────────────

    async def list_calls(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all calls, optionally filtered by date range."""
        client = self._ensure_client()
        return await self._fetch_all_calls(client, from_date=from_date, to_date=to_date)

    async def list_users(self) -> list[dict[str, Any]]:
        """Return all Gong users."""
        client = self._ensure_client()
        return await self._fetch_all_users(client)

    async def get_call(self, call_id: str) -> dict[str, Any]:
        """Return extended data for a single call."""
        client = self._ensure_client()
        return await with_retry(client.get_call, call_id)

    async def get_call_transcript(self, call_id: str) -> dict[str, Any]:
        """Return the transcript for a single call."""
        client = self._ensure_client()
        return await with_retry(client.get_call_transcripts, call_id)

    async def list_scorecards(self) -> list[dict[str, Any]]:
        """Return all scorecards from Gong settings."""
        client = self._ensure_client()
        result = await with_retry(client.get_scorecards)
        return result.get("scorecards", []) or []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> GongConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
