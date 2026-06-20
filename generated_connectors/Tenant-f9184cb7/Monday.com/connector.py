"""Monday.com connector — orchestration layer.

All HTTP calls    → client/http_client.py  (MondayComHTTPClient)
Normalization     → helpers/utils.py
Models            → models.py
Exceptions        → exceptions.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: Optional[Dict[str, Any]] = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

from client.http_client import MondayComHTTPClient
from exceptions import (
    MondayComAuthError,
    MondayComError,
    MondayComNetworkError,
    MondayComNotFoundError,
    MondayComRateLimitError,
)
from helpers.utils import normalize_board, normalize_item, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

CONNECTOR_TYPE: str = "monday_com"
AUTH_TYPE: str = "api_key"


class MondayComConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Monday.com via the GraphQL API v2.

    Authenticates with a raw API token (``api_key`` config key).
    """

    CONNECTOR_TYPE: str = "monday_com"
    CONNECTOR_NAME: str = "Monday.com"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            tenant_id=tenant_id,
            connector_id=connector_id,
            config=config or {},
        )
        self._http_client: Optional[MondayComHTTPClient] = None

    # ── Internal helpers ────────────────────────────────────────────────────────

    def _ensure_client(self) -> MondayComHTTPClient:
        if self._http_client is None:
            self._http_client = MondayComHTTPClient()
        return self._http_client

    def _get_api_key(self) -> str:
        return str(self.config.get("api_key", "")).strip()

    # ── Lifecycle ───────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release any held resources."""
        self._http_client = None

    async def __aenter__(self) -> "MondayComConnector":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    # ── install ─────────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that ``api_key`` is present in config.

        Returns :class:`InstallResult` — does NOT make a live API call
        (token validation happens in ``health_check``).
        """
        api_key = self._get_api_key()
        if not api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="api_key is required to connect to Monday.com",
            )
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Monday.com connector installed successfully",
        )

    # ── health_check ────────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Verify credentials by calling ``{ me { id name email } }``."""
        api_key = self._get_api_key()
        if not api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is not configured",
            )

        client = self._ensure_client()
        try:
            me = await client.get_me(api_key)
            name = me.get("name", "") or me.get("email", "") or "Unknown"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected as: {name}",
            )
        except MondayComAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Authentication failed: {exc}",
            )
        except (MondayComNetworkError, MondayComError) as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=f"Health check failed: {exc}",
            )

    # ── sync ────────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync all boards and their items from Monday.com.

        For each board:
          1. Normalize the board → ConnectorDocument (type="board")
          2. Paginate all items via cursor → each → ConnectorDocument (type="work_item")

        Returns :class:`SyncResult` with counts.
        """
        try:
            boards = await self.list_boards()
        except MondayComAuthError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Sync aborted — authentication error: {exc}",
            )
        except MondayComError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Sync aborted — error fetching boards: {exc}",
            )

        documents: List[ConnectorDocument] = []
        failed_boards = 0

        for board in boards:
            board_id = str(board.get("id", "")).strip()
            if not board_id:
                continue

            # Normalize the board itself
            documents.append(normalize_board(board))

            # Fetch and normalize all items for this board
            try:
                items = await self.list_board_items(board_id)
                for item in items:
                    documents.append(normalize_item(item, board_id))
            except MondayComError:
                failed_boards += 1

        synced = len(documents)
        status = (
            SyncStatus.FAILED
            if synced == 0 and failed_boards > 0
            else SyncStatus.PARTIAL
            if failed_boards > 0
            else SyncStatus.COMPLETED
        )

        return SyncResult(
            status=status,
            documents_found=synced,
            documents_synced=synced,
            documents_failed=failed_boards,
            message=(
                f"Synced {synced} document(s) from {len(boards)} board(s)"
                + (f" — {failed_boards} board(s) failed" if failed_boards else "")
            ),
        )

    # ── Domain methods ──────────────────────────────────────────────────────────

    async def list_boards(self, page: int = 1, limit: int = 50) -> List[Dict[str, Any]]:
        """Return all boards, paginating automatically."""
        client = self._ensure_client()
        api_key = self._get_api_key()
        all_boards: List[Dict[str, Any]] = []
        current_page = page

        while True:
            page_boards = await client.list_boards(api_key, page=current_page, limit=limit)
            if not page_boards:
                break
            all_boards.extend(page_boards)
            if len(page_boards) < limit:
                break
            current_page += 1

        return all_boards

    async def get_board(self, board_id: str) -> Dict[str, Any]:
        """Return a single board dict including groups and columns."""
        client = self._ensure_client()
        return await client.get_board(self._get_api_key(), board_id)

    async def list_board_items(
        self, board_id: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Return all items from a board, following cursor pagination."""
        client = self._ensure_client()
        api_key = self._get_api_key()
        all_items: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            page = await client.list_board_items(
                api_key, board_id, limit=limit, cursor=cursor
            )
            items = page.get("items") or []
            all_items.extend(items)
            cursor = page.get("cursor")
            if not cursor or not items:
                break

        return all_items

    async def get_item(self, item_id: str) -> Dict[str, Any]:
        """Return a single item dict with board info and column values."""
        client = self._ensure_client()
        return await client.get_item(self._get_api_key(), item_id)

    async def list_teams(self) -> List[Dict[str, Any]]:
        """Return all teams in the account."""
        client = self._ensure_client()
        return await client.list_teams(self._get_api_key())

    async def list_users(self, page: int = 1, limit: int = 50) -> List[Dict[str, Any]]:
        """Return all users, paginating automatically."""
        client = self._ensure_client()
        api_key = self._get_api_key()
        all_users: List[Dict[str, Any]] = []
        current_page = page

        while True:
            page_users = await client.list_users(
                api_key, page=current_page, limit=limit
            )
            if not page_users:
                break
            all_users.extend(page_users)
            if len(page_users) < limit:
                break
            current_page += 1

        return all_users
