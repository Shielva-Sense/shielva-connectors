"""Shielva connector for Front — team email / shared inbox."""
from __future__ import annotations

from typing import Any

from shared.base_connector import BaseConnector

from client import FrontHTTPClient
from exceptions import FrontAuthError, FrontError, FrontNetworkError
from helpers import (
    normalize_contact,
    normalize_conversation,
    normalize_message,
    normalize_teammate,
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

CONNECTOR_TYPE: str = "front"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_LIMIT: int = 100


class FrontConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Front (team email / shared inbox).

    Syncs conversations, contacts, teammates, inboxes, and tags via the
    Front Core API v1, authenticated with a Bearer API token.
    """

    CONNECTOR_TYPE: str = "front"
    AUTH_TYPE: str = "api_key"

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

        self._api_token: str = _config.get("api_token", "").strip()
        self._http_client: FrontHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> FrontHTTPClient:
        return FrontHTTPClient(config=self.config)

    def _ensure_client(self) -> FrontHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not self._api_token

    def _tenant(self) -> str:
        return getattr(self, "tenant_id", "") or getattr(self, "_tenant_id", "")

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the API token by calling GET /me."""
        if self._missing_creds():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: api_token",
            )

        client = self._make_client()
        try:
            me = await with_retry(client.get_me)
            await client.aclose()
            first: str = me.get("first_name", "") or ""
            last: str = me.get("last_name", "") or ""
            email: str = me.get("email", "") or ""
            name = f"{first} {last}".strip() or email or "Unknown teammate"
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Front as {name}",
            )
        except FrontAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
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
        """Ping GET /me and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_me)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Front API is reachable",
            )
        except FrontAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except FrontNetworkError as exc:
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

    async def sync(
        self,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync conversations and contacts from Front.

        All cursor pagination is handled internally.  Documents are optionally
        ingested into the knowledge base when ``kb_id`` is provided.
        """
        if self._http_client is None:
            self._http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0
        tenant = self._tenant()

        # Sync conversations
        try:
            page_token: str | None = None
            while True:
                resp = await with_retry(
                    self._http_client.get_conversations,
                    page_token=page_token,
                    limit=SYNC_PAGE_LIMIT,
                )
                items: list[dict[str, Any]] = resp.get("_results", []) or []
                if not items:
                    break
                found += len(items)

                for raw in items:
                    try:
                        doc = normalize_conversation(raw)
                        doc.connector_id = self.connector_id
                        doc.tenant_id = tenant
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                next_token: str | None = (
                    resp.get("_pagination", {}).get("next") or None
                )
                if not next_token:
                    break
                page_token = next_token

        except FrontError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # Sync contacts
        try:
            page_token = None
            while True:
                resp = await with_retry(
                    self._http_client.get_contacts,
                    page_token=page_token,
                    limit=SYNC_PAGE_LIMIT,
                )
                items = resp.get("_results", []) or []
                if not items:
                    break
                found += len(items)

                for raw in items:
                    try:
                        doc = normalize_contact(raw)
                        doc.connector_id = self.connector_id
                        doc.tenant_id = tenant
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                next_token = resp.get("_pagination", {}).get("next") or None
                if not next_token:
                    break
                page_token = next_token

        except FrontError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Contacts sync failed: {exc}",
            )

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

    # ── Conversations ─────────────────────────────────────────────────────────

    async def list_conversations(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all conversations (auto-paginated)."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            resp = await with_retry(
                client.get_conversations,
                page_token=page_token,
                limit=SYNC_PAGE_LIMIT,
                **kwargs,
            )
            items: list[dict[str, Any]] = resp.get("_results", []) or []
            results.extend(items)
            next_token: str | None = resp.get("_pagination", {}).get("next") or None
            if not next_token:
                break
            page_token = next_token
        return results

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Return a single conversation by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_conversation, conversation_id)

    async def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return all messages in a conversation."""
        client = self._ensure_client()
        resp = await with_retry(client.get_conversation_messages, conversation_id)
        return resp.get("_results", []) or []

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all contacts (auto-paginated)."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            resp = await with_retry(
                client.get_contacts,
                page_token=page_token,
                limit=SYNC_PAGE_LIMIT,
            )
            items: list[dict[str, Any]] = resp.get("_results", []) or []
            results.extend(items)
            next_token: str | None = resp.get("_pagination", {}).get("next") or None
            if not next_token:
                break
            page_token = next_token
        return results

    # ── Teammates ──────────────────────────────────────────────────────────────

    async def list_teammates(self) -> list[dict[str, Any]]:
        """Return all teammates."""
        client = self._ensure_client()
        resp = await with_retry(client.get_teammates)
        return resp.get("_results", []) or []

    # ── Inboxes ───────────────────────────────────────────────────────────────

    async def list_inboxes(self) -> list[dict[str, Any]]:
        """Return all inboxes."""
        client = self._ensure_client()
        resp = await with_retry(client.get_inboxes)
        return resp.get("_results", []) or []

    # ── Tags ─────────────────────────────────────────────────────────────────

    async def list_tags(self) -> list[dict[str, Any]]:
        """Return all tags."""
        client = self._ensure_client()
        resp = await with_retry(client.get_tags)
        return resp.get("_results", []) or []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> FrontConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
