"""Monday.com connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

try:
    from shared.base_connector import (
        AuthStatus,
        BaseConnector,
        ConnectorHealth,
        ConnectorStatus,
        NormalizedDocument,
        SyncResult,
        SyncStatus,
    )
    _BASE = BaseConnector
    _HAS_SDK = True
except ImportError:
    _BASE = object  # type: ignore[assignment,misc]
    _HAS_SDK = False

from client.http_client import MondayHTTPClient
from exceptions import MondayAuthError, MondayError, MondayNetworkError
from helpers.utils import normalize_board, normalize_item, with_retry
from models import (
    AuthStatus as _LocalAuthStatus,
    ConnectorHealth as _LocalConnectorHealth,
    ConnectorDocument,
    InstallResult,
    HealthCheckResult,
    SyncResult as _LocalSyncResult,
    SyncStatus as _LocalSyncStatus,
)

logger = structlog.get_logger(__name__)

_MONDAY_API_URL = "https://api.monday.com/v2"
_DEFAULT_BOARD_LIMIT = 50
_DEFAULT_ITEM_LIMIT = 50


class MondayConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for Monday.com via the Monday.com GraphQL API v2."""

    CONNECTOR_TYPE = "monday"
    CONNECTOR_NAME = "Monday.com"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS = ["api_token"]

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        if _HAS_SDK:
            super().__init__(tenant_id, connector_id, cfg)
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = cfg
        self._http_client: Optional[MondayHTTPClient] = None

    def _ensure_client(self) -> MondayHTTPClient:
        if self._http_client is None:
            self._http_client = MondayHTTPClient(api_url=_MONDAY_API_URL)
        return self._http_client

    def _get_token(self) -> str:
        return self.config.get("api_token", "")

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        """Validate that api_token is present; returns InstallResult."""
        api_token = self.config.get("api_token")

        if not api_token:
            logger.warning(
                "monday.install.missing_credentials",
                connector_id=self.connector_id,
            )
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,  # type: ignore[name-defined]
                    auth_status=AuthStatus.MISSING_CREDENTIALS,  # type: ignore[name-defined]
                    message="api_token is required",
                )
            return InstallResult(
                health=_LocalConnectorHealth.OFFLINE,
                auth_status=_LocalAuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="api_token is required",
            )

        logger.info("monday.install.ok", connector_id=self.connector_id)

        if _HAS_SDK:
            return ConnectorStatus(  # type: ignore[name-defined]
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                message="Connector installed — API token present",
            )
        return InstallResult(
            health=_LocalConnectorHealth.HEALTHY,
            auth_status=_LocalAuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Connector installed — API token present",
        )

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        """Run `{ me { name email } }` to verify the API token."""
        token = self._get_token()
        try:
            me = await with_retry(
                lambda: self._ensure_client().get_me(token),
                max_attempts=2,
            )
            name = me.get("name") or me.get("email") or "unknown user"
            msg = f"Connected as: {name}"

            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                    auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                    message=msg,
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.HEALTHY,
                auth_status=_LocalAuthStatus.CONNECTED,
                message=msg,
            )
        except MondayAuthError as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.INVALID_CREDENTIALS,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.FAILED,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.FAILED,
                message=str(exc),
            )

    # ── sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: Optional[Any] = None,
        kb_id: str = "",
    ) -> Any:
        """Sync all boards and their items from Monday.com.

        Fetches all boards (paginated), then for each board fetches all items
        (cursor-based pagination). Normalizes each item to a ConnectorDocument.
        """
        token = self._get_token()

        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            boards = await self.list_boards(limit=_DEFAULT_BOARD_LIMIT)

            for board in boards:
                board_id = str(board.get("id", ""))
                board_name = board.get("name", board_id)
                if not board_id:
                    continue

                try:
                    items = await self.list_items(
                        board_id=board_id,
                        limit=_DEFAULT_ITEM_LIMIT,
                    )
                    documents_found += len(items)

                    for item in items:
                        try:
                            doc = normalize_item(
                                item,
                                board_id,
                                board_name,
                                self.connector_id,
                                self.tenant_id,
                            )
                            if _HAS_SDK:
                                normalized = NormalizedDocument(  # type: ignore[name-defined]
                                    id=doc.id,
                                    source_id=f"{board_id}:{item.get('id', '')}",
                                    title=doc.title,
                                    content=doc.content,
                                    content_type="text",
                                    source_url="",
                                    author="",
                                    source="monday",
                                    tenant_id=self.tenant_id,
                                    connector_id=self.connector_id,
                                    metadata=doc.metadata,
                                )
                                await self.ingest_document(normalized, kb_id=kb_id or "")
                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "monday.sync.item_failed",
                                board_id=board_id,
                                item_id=item.get("id", ""),
                                error=str(exc),
                            )
                            documents_failed += 1

                except MondayAuthError:
                    raise
                except Exception as exc:
                    logger.error(
                        "monday.sync.board_failed",
                        board_id=board_id,
                        error=str(exc),
                    )
                    documents_failed += 1

            status = (
                _LocalSyncStatus.COMPLETED
                if documents_failed == 0
                else _LocalSyncStatus.PARTIAL
            )
            msg = (
                f"Synced {documents_synced}/{documents_found} items "
                f"from {len(boards)} boards"
            )

            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=msg,
                )
            return _LocalSyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=msg,
            )

        except Exception as exc:
            logger.error(
                "monday.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.FAILED,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=str(exc),
                )
            return _LocalSyncResult(
                status=_LocalSyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── convenience methods ───────────────────────────────────────────────────

    async def list_boards(
        self,
        limit: int = _DEFAULT_BOARD_LIMIT,
    ) -> List[Dict[str, Any]]:
        """List all boards via paginated boards query."""
        token = self._get_token()
        boards: List[Dict[str, Any]] = []
        page = 1

        while True:
            page_boards = await with_retry(
                lambda p=page: self._ensure_client().get_boards(
                    token, limit=limit, page=p
                ),
                max_attempts=3,
            )
            boards.extend(page_boards)
            if len(page_boards) < limit:
                break
            page += 1

        return boards

    async def get_board(self, board_id: str) -> Dict[str, Any]:
        """Fetch a single board by ID with items and column values."""
        token = self._get_token()
        return await with_retry(
            lambda: self._ensure_client().get_board(token, board_id=board_id),
            max_attempts=3,
        )

    async def list_items(
        self,
        board_id: str,
        limit: int = _DEFAULT_ITEM_LIMIT,
    ) -> List[Dict[str, Any]]:
        """List all items from a board via cursor-based pagination."""
        token = self._get_token()
        items: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            page = await with_retry(
                lambda c=cursor: self._ensure_client().get_items_page(
                    token, board_id=board_id, limit=limit, cursor=c
                ),
                max_attempts=3,
            )
            page_items = page.get("items") or []
            items.extend(page_items)
            cursor = page.get("cursor")
            if not cursor or len(page_items) < limit:
                break

        return items

    async def get_item(self, item_id: str) -> Dict[str, Any]:
        """Fetch a single item by ID with column values."""
        token = self._get_token()
        return await with_retry(
            lambda: self._ensure_client().get_item(token, item_id=item_id),
            max_attempts=3,
        )

    async def list_workspaces(self) -> List[Dict[str, Any]]:
        """List all workspaces with id and name."""
        token = self._get_token()
        return await with_retry(
            lambda: self._ensure_client().get_workspaces(token),
            max_attempts=3,
        )

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> "MondayConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
