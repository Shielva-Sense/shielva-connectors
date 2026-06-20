from __future__ import annotations

from typing import Any, Dict

from client import TrelloHTTPClient
from exceptions import TrelloAuthError, TrelloError, TrelloNetworkError
from helpers import normalize_board, normalize_card, with_retry
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

CONNECTOR_TYPE: str = "trello"
AUTH_TYPE: str = "api_key"


class TrelloConnector(BaseConnector):
    """
    Shielva connector for Trello (Kanban-style project management by Atlassian).

    Authenticates using an API key + OAuth token appended as query parameters
    (?key=...&token=...) on every request — Trello does not use the Authorization
    header.

    Syncs boards and cards from the authenticated member's account. Also provides
    direct access to board lists, members, and labels.

    Install fields:
        ``api_key`` — Trello API Key (from trello.com/app-key)
        ``token``   — Trello OAuth Token (generated from API Key page)
    """

    CONNECTOR_TYPE: str = "trello"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._api_key: str = _config.get("api_key", "").strip()
        self._token: str = _config.get("token", "").strip()
        self._http_client: TrelloHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> TrelloHTTPClient:
        return TrelloHTTPClient(api_key=self._api_key, token=self._token)

    def _ensure_client(self) -> TrelloHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not (self._api_key and self._token)

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key + token by calling GET /members/me."""
        if self._missing_creds():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Both api_key and token are required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_member, "me")
            await client.aclose()
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Trello",
            )
        except TrelloAuthError as exc:
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
        """Ping GET /members/me and return health with username and full name."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key and token are required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_member, "me")
            await client.aclose()
            username: str = data.get("username", "") if isinstance(data, dict) else ""
            full_name: str = data.get("fullName", "") if isinstance(data, dict) else ""
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Trello API is reachable (user: {username or full_name})",
                username=username,
                full_name=full_name,
            )
        except TrelloAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except TrelloNetworkError as exc:
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
        """
        Sync boards and cards from the authenticated Trello member.

        Fetches all open boards, normalizes each board into a ConnectorDocument,
        then fetches all open cards for each board and normalizes them too.
        """
        if self._http_client is None:
            self._http_client = self._make_client()

        kb_id: str = kwargs.get("kb_id", "")
        found = 0
        synced = 0
        failed = 0

        # Fetch boards
        try:
            boards = await with_retry(self._http_client.list_boards, "me", "open")
        except TrelloError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to list boards: {exc}",
            )

        for board in boards:
            board_id: str = board.get("id", "") or ""
            if not board_id:
                continue

            # Normalize board document
            found += 1
            try:
                board_doc = normalize_board(board)
                if kb_id:
                    await self._ingest_document(board_doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

            # Fetch and normalize cards for this board
            try:
                cards = await with_retry(
                    self._http_client.list_board_cards, board_id, "open"
                )
            except TrelloError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=f"Failed to list cards for board {board_id}: {exc}",
                )

            found += len(cards)
            for card in cards:
                try:
                    doc = normalize_card(card, board_id)
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

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Board methods ─────────────────────────────────────────────────────────

    async def list_boards(self, filter: str = "open") -> list[dict[str, Any]]:
        """Return list of board dicts for the authenticated member."""
        client = self._ensure_client()
        return await with_retry(client.list_boards, "me", filter)

    async def get_board(self, board_id: str) -> dict[str, Any]:
        """Return a single board dict by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_board, board_id)

    # ── List methods ──────────────────────────────────────────────────────────

    async def list_board_lists(
        self, board_id: str, filter: str = "open"
    ) -> list[dict[str, Any]]:
        """Return list of list dicts for a board."""
        client = self._ensure_client()
        return await with_retry(client.list_board_lists, board_id, filter)

    # ── Card methods ──────────────────────────────────────────────────────────

    async def list_board_cards(
        self, board_id: str, filter: str = "open"
    ) -> list[dict[str, Any]]:
        """Return list of card dicts for a board."""
        client = self._ensure_client()
        return await with_retry(client.list_board_cards, board_id, filter)

    async def get_card(self, card_id: str) -> dict[str, Any]:
        """Return a single card dict by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_card, card_id)

    # ── Member methods ────────────────────────────────────────────────────────

    async def list_board_members(self, board_id: str) -> list[dict[str, Any]]:
        """Return list of member dicts for a board."""
        client = self._ensure_client()
        return await with_retry(client.list_board_members, board_id)

    # ── Label methods ─────────────────────────────────────────────────────────

    async def list_board_labels(self, board_id: str) -> list[dict[str, Any]]:
        """Return list of label dicts for a board."""
        client = self._ensure_client()
        return await with_retry(client.list_board_labels, board_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> TrelloConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
