from __future__ import annotations

from typing import Any, Dict

from client import IntercomHTTPClient
from exceptions import IntercomAuthError, IntercomError, IntercomNetworkError
from helpers import normalize_contact, normalize_conversation, with_retry
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

SYNC_PAGE_SIZE: int = 150
SYNC_CONV_PAGE_SIZE: int = 20
CONNECTOR_TYPE: str = "intercom"
AUTH_TYPE: str = "api_key"


class IntercomConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Intercom.

    Syncs contacts (leads + users) and conversations from the
    Intercom REST API v2.10 using Bearer token authentication.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._tenant_id: str = tenant_id

        self._access_token: str = _config.get("access_token", "")
        self._http_client: IntercomHTTPClient | None = None

    def _make_client(self) -> IntercomHTTPClient:
        return IntercomHTTPClient()

    def _ensure_client(self) -> IntercomHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._access_token:
            missing.append("access_token")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install credentials by calling GET /me."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_me,
                self._access_token,
            )
            admin_name: str = data.get("name", "")
            admin_email: str = data.get("email", "")
            display = admin_name or admin_email or "Intercom Admin"
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Intercom as {display}",
            )
        except IntercomAuthError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /me and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_me,
                self._access_token,
            )
            admin_name: str = data.get("name", "")
            admin_email: str = data.get("email", "")
            display = admin_name or admin_email or "Intercom Admin"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Intercom API reachable. Admin: {display}",
            )
        except IntercomAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except IntercomNetworkError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync Intercom contacts and conversations into the knowledge base.

        Follows Intercom's cursor-based pagination (``pages.next.starting_after``)
        for both contacts and conversations. ``full`` is accepted for interface
        compatibility.
        """
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        # ── Contacts ──────────────────────────────────────────────────────────
        starting_after: str | None = None
        while True:
            try:
                page_data = await with_retry(
                    client.list_contacts,
                    self._access_token,
                    per_page=SYNC_PAGE_SIZE,
                    starting_after=starting_after,
                )
            except IntercomError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            raw_data: Any = page_data.get("data", [])
            contacts: list[dict[str, Any]] = raw_data if isinstance(raw_data, list) else []
            found += len(contacts)

            for contact in contacts:
                try:
                    doc = normalize_contact(
                        contact,
                        self.connector_id,
                        self._tenant_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # Follow cursor pagination
            pages: dict[str, Any] = page_data.get("pages", {}) or {}
            next_page: Any = pages.get("next")
            if not contacts or not next_page:
                break
            if isinstance(next_page, dict):
                starting_after = next_page.get("starting_after")
            else:
                break
            if not starting_after:
                break

        # ── Conversations ──────────────────────────────────────────────────────
        conv_cursor: str | None = None
        while True:
            try:
                conv_data = await with_retry(
                    client.list_conversations,
                    self._access_token,
                    per_page=SYNC_CONV_PAGE_SIZE,
                    starting_after=conv_cursor,
                )
            except IntercomError:
                # Don't fail the whole sync if conversations error — just stop
                break

            conv_list: list[dict[str, Any]] = conv_data.get("conversations", [])
            found += len(conv_list)

            for conversation in conv_list:
                try:
                    doc = normalize_conversation(
                        conversation,
                        self.connector_id,
                        self._tenant_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            conv_pages: dict[str, Any] = conv_data.get("pages", {}) or {}
            conv_next: Any = conv_pages.get("next")
            if not conv_list or not conv_next:
                break
            if isinstance(conv_next, dict):
                conv_cursor = conv_next.get("starting_after")
            else:
                break
            if not conv_cursor:
                break

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        per_page: int = 150,
        starting_after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a flat list of all Intercom contacts (leads + users).

        Follows cursor-based pagination automatically until all pages are fetched.
        """
        client = self._ensure_client()
        all_contacts: list[dict[str, Any]] = []
        cursor: str | None = starting_after

        while True:
            page_data = await with_retry(
                client.list_contacts,
                self._access_token,
                per_page=per_page,
                starting_after=cursor,
            )
            raw: Any = page_data.get("data", [])
            batch: list[dict[str, Any]] = raw if isinstance(raw, list) else []
            all_contacts.extend(batch)

            pages: dict[str, Any] = page_data.get("pages", {}) or {}
            next_page: Any = pages.get("next")
            if not batch or not next_page:
                break
            if isinstance(next_page, dict):
                cursor = next_page.get("starting_after")
            else:
                break
            if not cursor:
                break

        return all_contacts

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        """Return a single contact dict by ID."""
        client = self._ensure_client()
        return await with_retry(
            client.get_contact,
            self._access_token,
            contact_id,
        )

    # ── Conversations ─────────────────────────────────────────────────────────

    async def list_conversations(
        self,
        per_page: int = 20,
        starting_after: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a flat list of all Intercom conversations.

        Follows cursor-based pagination automatically.
        """
        client = self._ensure_client()
        all_conversations: list[dict[str, Any]] = []
        cursor: str | None = starting_after

        while True:
            page_data = await with_retry(
                client.list_conversations,
                self._access_token,
                per_page=per_page,
                starting_after=cursor,
            )
            batch: list[dict[str, Any]] = page_data.get("conversations", [])
            all_conversations.extend(batch)

            pages: dict[str, Any] = page_data.get("pages", {}) or {}
            next_page: Any = pages.get("next")
            if not batch or not next_page:
                break
            if isinstance(next_page, dict):
                cursor = next_page.get("starting_after")
            else:
                break
            if not cursor:
                break

        return all_conversations

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Return a single conversation dict by ID."""
        client = self._ensure_client()
        return await with_retry(
            client.get_conversation,
            self._access_token,
            conversation_id,
        )

    # ── Companies ─────────────────────────────────────────────────────────────

    async def list_companies(self) -> list[dict[str, Any]]:
        """Return a flat list of all Intercom companies."""
        client = self._ensure_client()
        page_data = await with_retry(
            client.list_companies,
            self._access_token,
        )
        raw: Any = page_data.get("data", [])
        return raw if isinstance(raw, list) else []

    # ── Admins ────────────────────────────────────────────────────────────────

    async def list_admins(self) -> list[dict[str, Any]]:
        """Return a list of all admins in the Intercom workspace."""
        client = self._ensure_client()
        page_data = await with_retry(
            client.list_admins,
            self._access_token,
        )
        # Intercom returns {"type": "admin.list", "admins": [...]}
        raw: Any = page_data.get("admins", page_data.get("data", []))
        return raw if isinstance(raw, list) else []

    # ── Tags ──────────────────────────────────────────────────────────────────

    async def list_tags(self) -> list[dict[str, Any]]:
        """Return a list of all tags in the Intercom workspace."""
        client = self._ensure_client()
        page_data = await with_retry(
            client.list_tags,
            self._access_token,
        )
        raw: Any = page_data.get("data", [])
        return raw if isinstance(raw, list) else []

    # ── Segments ──────────────────────────────────────────────────────────────

    async def list_segments(self) -> list[dict[str, Any]]:
        """Return a list of all segments in the Intercom workspace."""
        client = self._ensure_client()
        page_data = await with_retry(
            client.list_segments,
            self._access_token,
        )
        raw: Any = page_data.get("segments", page_data.get("data", []))
        return raw if isinstance(raw, list) else []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> IntercomConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
