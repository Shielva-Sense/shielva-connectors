from __future__ import annotations

import urllib.parse
from typing import Any

from client import DriftHTTPClient
from exceptions import DriftAuthError, DriftError, DriftNetworkError
from helpers import (
    normalize_account,
    normalize_contact,
    normalize_conversation,
    normalize_message,
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

CONNECTOR_TYPE: str = "drift"
AUTH_TYPE: str = "oauth2"

DRIFT_AUTH_URL: str = "https://dev.drift.com/authorize"
SYNC_PAGE_SIZE: int = 50
CONTACT_PAGE_SIZE: int = 100


class DriftConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Drift (conversational marketing platform).

    Syncs conversations, contacts, accounts, and messages from the
    Drift REST API v1 via OAuth 2.0 Authorization Code flow.
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
        if not isinstance(BaseConnector, type) or BaseConnector.__name__ == "BaseConnector":
            try:
                super().__init__(  # type: ignore[misc]
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

        self._access_token: str = _config.get("access_token", "")
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._http_client: DriftHTTPClient | None = None

    def _make_client(self) -> DriftHTTPClient:
        return DriftHTTPClient(config=self.config)

    def _ensure_client(self) -> DriftHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._access_token:
            missing.append("access_token")
        return missing

    # ── OAuth2 ────────────────────────────────────────────────────────────────

    async def authorize(self) -> str:
        """Build and return the OAuth 2.0 authorization URL for Drift."""
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        query_string = urllib.parse.urlencode(params)
        return f"{DRIFT_AUTH_URL}?{query_string}"

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install credentials by calling GET /users/list."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_users)
            users: list[dict[str, Any]] = data.get("data", {}).get("users", []) or []
            if users:
                first_user = users[0]
                display_name: str = (
                    first_user.get("name", "")
                    or first_user.get("email", "")
                    or "Drift User"
                )
            else:
                display_name = "Drift Workspace"
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Drift as {display_name}",
            )
        except DriftAuthError as exc:
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
        """Ping GET /users/list and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_users)
            users: list[dict[str, Any]] = data.get("data", {}).get("users", []) or []
            if users:
                first_user = users[0]
                display_name: str = (
                    first_user.get("name", "")
                    or first_user.get("email", "")
                    or "Drift User"
                )
            else:
                display_name = "Drift Workspace"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Drift API reachable. User: {display_name}",
            )
        except DriftAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except DriftNetworkError as exc:
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
        """Sync conversations, contacts, accounts, and messages from Drift.

        Paginates through all resources using cursor-based pagination and
        normalizes each into a ConnectorDocument.
        """
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        # Sync conversations
        next_token: str | None = None
        while True:
            try:
                page_data = await with_retry(
                    client.get_conversations,
                    limit=SYNC_PAGE_SIZE,
                    next_page_token=next_token,
                )
            except DriftError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            conversations: list[dict[str, Any]] = (
                page_data.get("data", {}).get("conversations", []) or []
            )
            found += len(conversations)

            for conv in conversations:
                try:
                    doc = normalize_conversation(
                        conv, self.connector_id, self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            pagination: dict[str, Any] = page_data.get("data", {}).get("pagination", {}) or {}
            next_token = pagination.get("next_page_token")
            if not conversations or not next_token:
                break

        # Sync contacts
        contact_token: str | None = None
        while True:
            try:
                contact_data = await with_retry(
                    client.get_contacts,
                    limit=CONTACT_PAGE_SIZE,
                    next_page_token=contact_token,
                )
            except DriftError:
                break

            contacts: list[dict[str, Any]] = (
                contact_data.get("data", {}).get("contacts", []) or []
            )
            found += len(contacts)

            for contact in contacts:
                try:
                    doc = normalize_contact(
                        contact, self.connector_id, self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            c_pagination: dict[str, Any] = (
                contact_data.get("data", {}).get("pagination", {}) or {}
            )
            contact_token = c_pagination.get("next_page_token")
            if not contacts or not contact_token:
                break

        # Sync accounts
        try:
            acct_data = await with_retry(client.get_accounts)
            accounts: list[dict[str, Any]] = (
                acct_data.get("data", {}).get("accounts", []) or []
            )
            found += len(accounts)
            for acct in accounts:
                try:
                    doc = normalize_account(
                        acct, self.connector_id, self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except DriftError:
            pass

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

    async def list_conversations(
        self,
        limit: int = 50,
        next_page_token: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return a paginated list of Drift conversations."""
        client = self._ensure_client()
        data = await with_retry(
            client.get_conversations,
            limit=limit,
            next_page_token=next_page_token,
        )
        return data.get("data", {}).get("conversations", []) or []

    async def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        """Return a single conversation by ID."""
        client = self._ensure_client()
        data = await with_retry(client.get_conversation, conversation_id)
        return data.get("data", {}) or data

    async def get_conversation_messages(
        self, conversation_id: int
    ) -> list[dict[str, Any]]:
        """Return all messages for a conversation."""
        client = self._ensure_client()
        data = await with_retry(client.get_conversation_messages, conversation_id)
        return data.get("data", {}).get("messages", []) or []

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        limit: int = 100,
        next_page_token: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return a paginated list of Drift contacts."""
        client = self._ensure_client()
        data = await with_retry(
            client.get_contacts,
            limit=limit,
            next_page_token=next_page_token,
        )
        return data.get("data", {}).get("contacts", []) or []

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def list_accounts(self) -> list[dict[str, Any]]:
        """Return all Drift accounts."""
        client = self._ensure_client()
        data = await with_retry(client.get_accounts)
        return data.get("data", {}).get("accounts", []) or []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> DriftConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
