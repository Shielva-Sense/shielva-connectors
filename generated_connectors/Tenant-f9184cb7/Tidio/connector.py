from __future__ import annotations

from typing import Any

from client import TidioHTTPClient
from exceptions import TidioAuthError, TidioError, TidioNetworkError
from helpers import (
    normalize_chatbot,
    normalize_conversation,
    normalize_visitor,
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

CONNECTOR_TYPE: str = "tidio"
AUTH_TYPE: str = "api_key"

SYNC_PAGE_SIZE: int = 50


class TidioConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Tidio (live chat and chatbot platform).

    Syncs conversations, visitors, and chatbots from the Tidio REST API v1
    using API key Bearer token authentication.
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

        self._api_key: str = _config.get("api_key", "")
        self._http_client: TidioHTTPClient | None = None

    def _make_client(self) -> TidioHTTPClient:
        return TidioHTTPClient(config=self.config)

    def _ensure_client(self) -> TidioHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._api_key:
            missing.append("api_key")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install credentials by calling GET /api/v1/project."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_project)
            project: dict[str, Any] = data.get("project", data) or {}
            project_name: str = (
                project.get("name", "")
                or project.get("domain", "")
                or "Tidio Project"
            )
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Tidio project: {project_name}",
            )
        except TidioAuthError as exc:
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
        """Ping GET /api/v1/project and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_project)
            project: dict[str, Any] = data.get("project", data) or {}
            project_name: str = (
                project.get("name", "")
                or project.get("domain", "")
                or "Tidio Project"
            )
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Tidio API reachable. Project: {project_name}",
            )
        except TidioAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except TidioNetworkError as exc:
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
        """Sync conversations, visitors, and chatbots from Tidio.

        Paginates through all resources using page-based pagination and
        normalizes each into a ConnectorDocument.
        """
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        # Sync conversations
        page = 1
        while True:
            try:
                page_data = await with_retry(
                    client.get_conversations,
                    page=page,
                    page_size=SYNC_PAGE_SIZE,
                )
            except TidioError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            conversations: list[dict[str, Any]] = (
                page_data.get("conversations", page_data.get("data", [])) or []
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

            meta: dict[str, Any] = page_data.get("meta", page_data.get("pages", {})) or {}
            total_pages: int = int(
                meta.get("total_pages", meta.get("last", 1)) or 1
            )
            next_page: Any = meta.get("next")
            if not conversations or page >= total_pages or next_page is None and page >= total_pages:
                break
            page += 1

        # Sync visitors
        visitor_page = 1
        while True:
            try:
                visitor_data = await with_retry(
                    client.get_visitors,
                    page=visitor_page,
                    page_size=SYNC_PAGE_SIZE,
                )
            except TidioError:
                break

            visitors: list[dict[str, Any]] = (
                visitor_data.get("visitors", visitor_data.get("data", [])) or []
            )
            found += len(visitors)

            for visitor in visitors:
                try:
                    doc = normalize_visitor(
                        visitor, self.connector_id, self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            v_meta: dict[str, Any] = (
                visitor_data.get("meta", visitor_data.get("pages", {})) or {}
            )
            v_total_pages: int = int(
                v_meta.get("total_pages", v_meta.get("last", 1)) or 1
            )
            v_next: Any = v_meta.get("next")
            if not visitors or visitor_page >= v_total_pages or v_next is None and visitor_page >= v_total_pages:
                break
            visitor_page += 1

        # Sync chatbots
        try:
            chatbot_data = await with_retry(client.get_chatbots)
            chatbots: list[dict[str, Any]] = (
                chatbot_data.get("chatbots", chatbot_data.get("data", [])) or []
            )
            found += len(chatbots)
            for chatbot in chatbots:
                try:
                    doc = normalize_chatbot(
                        chatbot, self.connector_id, self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except TidioError:
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
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return a paginated list of Tidio conversations."""
        client = self._ensure_client()
        data = await with_retry(
            client.get_conversations,
            page=page,
            page_size=page_size,
            status=status,
        )
        return data.get("conversations", data.get("data", [])) or []

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Return a single conversation by ID."""
        client = self._ensure_client()
        data = await with_retry(client.get_conversation, conversation_id)
        return data.get("conversation", data) or data

    async def get_conversation_messages(
        self, conversation_id: str
    ) -> list[dict[str, Any]]:
        """Return all messages for a conversation."""
        client = self._ensure_client()
        data = await with_retry(client.get_conversation_messages, conversation_id)
        return data.get("messages", data.get("data", [])) or []

    # ── Visitors ──────────────────────────────────────────────────────────────

    async def list_visitors(
        self,
        page: int = 1,
        page_size: int = 50,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return a paginated list of Tidio visitors."""
        client = self._ensure_client()
        data = await with_retry(
            client.get_visitors,
            page=page,
            page_size=page_size,
        )
        return data.get("visitors", data.get("data", [])) or []

    # ── Operators ─────────────────────────────────────────────────────────────

    async def list_operators(self) -> list[dict[str, Any]]:
        """Return all Tidio operators."""
        client = self._ensure_client()
        data = await with_retry(client.get_operators)
        return data.get("operators", data.get("data", [])) or []

    # ── Chatbots ──────────────────────────────────────────────────────────────

    async def list_chatbots(self) -> list[dict[str, Any]]:
        """Return all Tidio chatbots."""
        client = self._ensure_client()
        data = await with_retry(client.get_chatbots)
        return data.get("chatbots", data.get("data", [])) or []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> TidioConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
