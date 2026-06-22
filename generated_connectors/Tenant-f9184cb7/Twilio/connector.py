from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import TwilioHTTPClient
from exceptions import TwilioAuthError, TwilioError
from helpers import normalize_call, normalize_message, with_retry
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


class TwilioConnector(BaseConnector):
    """
    Shielva connector for Twilio.

    Provides authentication, health checks, full/incremental sync, and
    direct access to SMS messages, voice calls, and phone numbers.
    """

    CONNECTOR_TYPE: str = "twilio"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)

        self._account_sid: str = _config.get("account_sid", "")
        self._auth_token: str = _config.get("auth_token", "")
        self.http_client: TwilioHTTPClient | None = None

    def _make_client(self) -> TwilioHTTPClient:
        return TwilioHTTPClient()

    def _ensure_client(self) -> TwilioHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate account_sid + auth_token by calling GET /Accounts/{sid}.json."""
        if not self._account_sid or not self._auth_token:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="account_sid and auth_token are required",
            )
        client = self._make_client()
        try:
            data = await with_retry(
                client.get_account, self._account_sid, self._auth_token
            )
            await client.aclose()
            self.http_client = self._make_client()
            friendly_name: str = data.get("friendly_name", self._account_sid)
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or self._account_sid,
                message=f"Connected to Twilio account: {friendly_name}",
            )
        except TwilioAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Twilio credentials: {exc.message}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping Twilio /Accounts/{sid}.json and return current health."""
        if not self._account_sid or not self._auth_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="account_sid and auth_token are required",
            )
        client = self._make_client()
        try:
            data = await with_retry(
                client.get_account, self._account_sid, self._auth_token
            )
            await client.aclose()
            friendly_name: str = data.get("friendly_name", self._account_sid)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Twilio account: {friendly_name}",
            )
        except TwilioAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
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
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync Twilio SMS messages and voice calls into the knowledge base.

        full=True → fetch all records.
        since=<datetime> → fetch records created after that timestamp.
        """
        client = self._ensure_client()

        date_filter: str | None = None
        if not full and since:
            date_filter = since.strftime("%Y-%m-%d")

        found = 0
        synced = 0
        failed = 0

        # Sync messages
        try:
            async for msg in self._iter_messages(client, date_sent_after=date_filter):
                found += 1
                try:
                    doc = normalize_message(msg, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except TwilioError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Messages sync failed: {exc}",
            )

        # Sync calls
        try:
            async for call in self._iter_calls(client, start_time_after=date_filter):
                found += 1
                try:
                    doc = normalize_call(call, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except TwilioError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Calls sync failed: {exc}",
            )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    # ── Messages ─────────────────────────────────────────────────────────────

    async def list_messages(
        self,
        page_size: int = SYNC_PAGE_SIZE,
        date_sent_after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all messages, following next_page_uri pagination."""
        client = self._ensure_client()
        return [m async for m in self._iter_messages(client, page_size=page_size, date_sent_after=date_sent_after)]

    async def get_message(self, message_sid: str) -> dict[str, Any]:
        """Fetch a single message by SID."""
        client = self._ensure_client()
        return await with_retry(
            client.get_message, self._account_sid, self._auth_token, message_sid
        )

    # ── Calls ────────────────────────────────────────────────────────────────

    async def list_calls(
        self,
        page_size: int = SYNC_PAGE_SIZE,
        start_time_after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all calls, following next_page_uri pagination."""
        client = self._ensure_client()
        return [c async for c in self._iter_calls(client, page_size=page_size, start_time_after=start_time_after)]

    async def get_call(self, call_sid: str) -> dict[str, Any]:
        """Fetch a single call by SID."""
        client = self._ensure_client()
        return await with_retry(
            client.get_call, self._account_sid, self._auth_token, call_sid
        )

    # ── Recordings ────────────────────────────────────────────────────────────

    async def list_recordings(
        self,
        call_sid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all recordings, optionally filtered by call_sid."""
        client = self._ensure_client()
        data = await with_retry(
            client.list_recordings,
            self._account_sid,
            self._auth_token,
            call_sid,
        )
        return data.get("recordings", [])

    # ── Phone Numbers ─────────────────────────────────────────────────────────

    async def list_phone_numbers(self) -> list[dict[str, Any]]:
        """Return all incoming phone numbers for the account."""
        client = self._ensure_client()
        data = await with_retry(
            client.list_phone_numbers, self._account_sid, self._auth_token
        )
        return data.get("incoming_phone_numbers", [])

    # ── Internal iterators ───────────────────────────────────────────────────

    async def _iter_messages(
        self,
        client: TwilioHTTPClient,
        page_size: int = SYNC_PAGE_SIZE,
        date_sent_after: str | None = None,
    ):  # type: ignore[return]
        """Async generator yielding individual message records across all pages."""
        page_token: str | None = None
        while True:
            page = await with_retry(
                client.list_messages,
                self._account_sid,
                self._auth_token,
                page_size,
                page_token,
                date_sent_after,
            )
            messages: list[dict[str, Any]] = page.get("messages", [])
            for msg in messages:
                yield msg
            next_page: str | None = page.get("next_page_uri")
            if not next_page:
                break
            page_token = next_page

    async def _iter_calls(
        self,
        client: TwilioHTTPClient,
        page_size: int = SYNC_PAGE_SIZE,
        start_time_after: str | None = None,
    ):  # type: ignore[return]
        """Async generator yielding individual call records across all pages."""
        page_token: str | None = None
        while True:
            page = await with_retry(
                client.list_calls,
                self._account_sid,
                self._auth_token,
                page_size,
                page_token,
                start_time_after,
            )
            calls: list[dict[str, Any]] = page.get("calls", [])
            for call in calls:
                yield call
            next_page: str | None = page.get("next_page_uri")
            if not next_page:
                break
            page_token = next_page

    # ── Ingest stub ──────────────────────────────────────────────────────────

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> TwilioConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
