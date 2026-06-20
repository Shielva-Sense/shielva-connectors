from __future__ import annotations

from typing import Any

from client import HelpScoutHTTPClient
from exceptions import HelpScoutAuthError, HelpScoutError, HelpScoutNetworkError
from helpers import (
    normalize_conversation,
    normalize_customer,
    normalize_mailbox,
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


CONNECTOR_TYPE: str = "helpscout"
AUTH_TYPE: str = "oauth2"
SYNC_PAGE_SIZE: int = 50


class HelpScoutConnector(BaseConnector):
    """Shielva connector for Help Scout.

    Syncs conversations, customers, mailboxes, users, and tags from a
    Help Scout account using OAuth 2.0 Client Credentials authentication.
    """

    CONNECTOR_TYPE: str = "helpscout"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(
            tenant_id=tenant_id, connector_id=connector_id, config=_config
        )
        self._client_id: str = _config.get("client_id", "").strip()
        self._client_secret: str = _config.get("client_secret", "").strip()
        self.client: HelpScoutHTTPClient = HelpScoutHTTPClient(config=_config)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _make_client(self) -> HelpScoutHTTPClient:
        return HelpScoutHTTPClient(config=self.config)

    def _missing_creds(self) -> bool:
        return not self._client_id or not self._client_secret

    def _extract_items(
        self, response: dict[str, Any], resource_key: str
    ) -> list[dict[str, Any]]:
        """Extract items from a HAL+JSON response envelope."""
        embedded = response.get("_embedded", {}) or {}
        items = embedded.get(resource_key, []) or []
        return items if isinstance(items, list) else []

    def _has_next_page(self, response: dict[str, Any]) -> bool:
        """Return True when the HAL response contains a _links.next href."""
        links = response.get("_links", {}) or {}
        next_link = links.get("next", {}) or {}
        return bool(next_link.get("href", ""))

    def _enrich_doc(
        self, doc: ConnectorDocument, connector_id: str, tenant_id: str
    ) -> ConnectorDocument:
        """Stamp connector_id and tenant_id onto a normalized document."""
        doc.connector_id = connector_id
        doc.tenant_id = tenant_id
        return doc

    # ── Install ────────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate client_id + client_secret by calling GET /users/me."""
        if self._missing_creds():
            missing = []
            if not self._client_id:
                missing.append("client_id")
            if not self._client_secret:
                missing.append("client_secret")
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            await client.authenticate()
            me = await with_retry(client.get_me)
            await client.aclose()
            first: str = me.get("firstName", "") or ""
            last: str = me.get("lastName", "") or ""
            name: str = f"{first} {last}".strip() or me.get("email", "") or "Unknown user"
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Help Scout as {name}",
            )
        except HelpScoutAuthError as exc:
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

    # ── Health check ───────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /users/me and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )

        client = self._make_client()
        try:
            await client.authenticate()
            await with_retry(client.get_me)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Help Scout API is reachable",
            )
        except HelpScoutAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except HelpScoutNetworkError as exc:
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

    # ── Sync ───────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync conversations, customers, mailboxes, and users from Help Scout.

        full=True fetches all records from page 1.
        """
        found = 0
        synced = 0
        failed = 0

        await self.client.authenticate()

        # ── Conversations ──────────────────────────────────────────────────
        try:
            page = 1
            while True:
                resp = await with_retry(self.client.get_conversations, page=page)
                items = self._extract_items(resp, "conversations")
                if not items:
                    break
                found += len(items)
                for raw in items:
                    try:
                        doc = self._enrich_doc(
                            normalize_conversation(raw),
                            self.connector_id,
                            self.tenant_id,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if not self._has_next_page(resp):
                    break
                page += 1
        except HelpScoutError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # ── Customers ──────────────────────────────────────────────────────
        try:
            page = 1
            while True:
                resp = await with_retry(self.client.get_customers, page=page)
                items = self._extract_items(resp, "customers")
                if not items:
                    break
                found += len(items)
                for raw in items:
                    try:
                        doc = self._enrich_doc(
                            normalize_customer(raw),
                            self.connector_id,
                            self.tenant_id,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if not self._has_next_page(resp):
                    break
                page += 1
        except HelpScoutError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Customers sync failed: {exc}",
            )

        # ── Mailboxes ──────────────────────────────────────────────────────
        try:
            resp = await with_retry(self.client.get_mailboxes)
            items = self._extract_items(resp, "mailboxes")
            found += len(items)
            for raw in items:
                try:
                    doc = self._enrich_doc(
                        normalize_mailbox(raw),
                        self.connector_id,
                        self.tenant_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except HelpScoutError:
            pass  # mailboxes are supplemental — non-fatal

        # ── Users ──────────────────────────────────────────────────────────
        try:
            page = 1
            while True:
                resp = await with_retry(self.client.get_users, page=page)
                items = self._extract_items(resp, "users")
                if not items:
                    break
                found += len(items)
                for raw in items:
                    try:
                        doc = self._enrich_doc(
                            normalize_user(raw),
                            self.connector_id,
                            self.tenant_id,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                if not self._has_next_page(resp):
                    break
                page += 1
        except HelpScoutError:
            pass  # users are supplemental — non-fatal

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

    # ── Conversation methods ───────────────────────────────────────────────────

    async def list_conversations(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all conversations across pages (HAL pagination)."""
        await self.client.authenticate()
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await with_retry(self.client.get_conversations, page=page, **kwargs)
            items = self._extract_items(resp, "conversations")
            results.extend(items)
            if not self._has_next_page(resp) or not items:
                break
            page += 1
        return results

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Return a single conversation by ID."""
        await self.client.authenticate()
        return await with_retry(self.client.get_conversation, conversation_id)

    # ── Customer methods ───────────────────────────────────────────────────────

    async def list_customers(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all customers across pages (HAL pagination)."""
        await self.client.authenticate()
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await with_retry(self.client.get_customers, page=page)
            items = self._extract_items(resp, "customers")
            results.extend(items)
            if not self._has_next_page(resp) or not items:
                break
            page += 1
        return results

    # ── Mailbox methods ────────────────────────────────────────────────────────

    async def list_mailboxes(self) -> list[dict[str, Any]]:
        """Return all mailboxes."""
        await self.client.authenticate()
        resp = await with_retry(self.client.get_mailboxes)
        return self._extract_items(resp, "mailboxes")

    # ── User methods ───────────────────────────────────────────────────────────

    async def list_users(self) -> list[dict[str, Any]]:
        """Return all users across pages (HAL pagination)."""
        await self.client.authenticate()
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await with_retry(self.client.get_users, page=page)
            items = self._extract_items(resp, "users")
            results.extend(items)
            if not self._has_next_page(resp) or not items:
                break
            page += 1
        return results

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> HelpScoutConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
