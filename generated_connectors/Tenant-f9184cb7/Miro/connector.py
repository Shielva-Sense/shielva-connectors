"""Miro connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

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

from client.http_client import MiroHTTPClient
from exceptions import MiroAuthError, MiroError, MiroNetworkError, MiroNotFoundError
from helpers.utils import normalize_board, normalize_item, with_retry
from models import (
    AuthStatus as _LocalAuthStatus,
    ConnectorDocument,
    ConnectorHealth as _LocalConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult as _LocalSyncResult,
    SyncStatus as _LocalSyncStatus,
)

logger = structlog.get_logger(__name__)

CONNECTOR_TYPE = "miro"
AUTH_TYPE = "oauth2"

_MIRO_AUTH_URI = "https://miro.com/oauth/authorize"
_MIRO_TOKEN_URI = "https://api.miro.com/v1/oauth/token"
_MIRO_API_BASE = "https://api.miro.com/v2"

_REQUIRED_SCOPES = ["boards:read", "organizations:read", "team:read"]


class MiroConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for Miro via the Miro REST API v2."""

    CONNECTOR_TYPE = "miro"
    CONNECTOR_NAME = "Miro"
    AUTH_TYPE = "oauth2"
    AUTH_URI = _MIRO_AUTH_URI
    TOKEN_URI = _MIRO_TOKEN_URI

    REQUIRED_SCOPES: List[str] = _REQUIRED_SCOPES

    REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]

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
        self.client = MiroHTTPClient(config=self.config)

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        """Validate install-time config fields.

        Requires client_id and client_secret. Returns PENDING auth status
        since the OAuth flow must be completed to obtain an access token.
        """
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        if not client_id or not client_secret:
            logger.warning(
                "miro.install.missing_credentials",
                connector_id=self.connector_id,
            )
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,  # type: ignore[name-defined]
                    auth_status=AuthStatus.MISSING_CREDENTIALS,  # type: ignore[name-defined]
                    message="client_id and client_secret are required",
                )
            return InstallResult(
                health=_LocalConnectorHealth.OFFLINE,
                auth_status=_LocalAuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="client_id and client_secret are required",
            )

        logger.info("miro.install.ok", connector_id=self.connector_id)

        if _HAS_SDK:
            return ConnectorStatus(  # type: ignore[name-defined]
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                auth_status=AuthStatus.PENDING,  # type: ignore[name-defined]
                message="Connector installed — complete OAuth to connect",
            )
        return InstallResult(
            health=_LocalConnectorHealth.HEALTHY,
            auth_status=_LocalAuthStatus.PENDING,
            connector_id=self.connector_id,
            message="Connector installed — complete OAuth to connect",
        )

    # ── authorize ─────────────────────────────────────────────────────────────

    async def authorize(
        self,
        state: Optional[str] = None,
    ) -> str:
        """Return the Miro OAuth2 authorization URL.

        The user must be redirected to this URL to complete the OAuth flow.
        After authorization, Miro redirects to redirect_uri with a code param
        that can be exchanged for tokens at TOKEN_URI.
        """
        client_id = self.config.get("client_id", "")
        redirect_uri = self.config.get("redirect_uri", "")

        params: Dict[str, str] = {
            "response_type": "code",
            "client_id": client_id,
        }
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        if state:
            params["state"] = state

        return f"{_MIRO_AUTH_URI}?{urlencode(params)}"

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        """Introspect the OAuth token via GET /v2/oauth-token.

        Returns HEALTHY when the token is valid. Returns DEGRADED with
        INVALID_CREDENTIALS on auth failure, FAILED on other errors.
        """
        try:
            data = await with_retry(
                lambda: self.client.get_token_info(),
                max_attempts=2,
            )
            # Miro returns user.name, team.name, scopes etc.
            user: Dict[str, Any] = data.get("user", {})
            user_name: str = user.get("name", "")
            team: Dict[str, Any] = data.get("team", {})
            team_name: str = team.get("name", "")

            msg = f"Connected"
            if user_name:
                msg += f" — user: {user_name}"
            if team_name:
                msg += f", team: {team_name}"

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
                user_name=user_name,
            )
        except MiroAuthError as exc:
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
        **kwargs: Any,
    ) -> Any:
        """Sync all Miro boards and their items.

        For each board accessible via the OAuth token:
        1. Fetch board metadata → normalize → (optionally) ingest
        2. Fetch all board items with cursor pagination → normalize → ingest

        Returns SyncResult with counts for found/synced/failed.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Fetch all boards (cursor-paginated)
            boards = await self.list_boards()
            all_docs: List[ConnectorDocument] = []

            # Normalize boards
            for board_raw in boards:
                doc = normalize_board(board_raw)
                all_docs.append(doc)

            # Normalize board items for each board
            for board_raw in boards:
                board_id: str = board_raw.get("id", "")
                if not board_id:
                    continue
                try:
                    items = await self.list_board_items(board_id)
                    for item_raw in items:
                        item_doc = normalize_item(item_raw, board_id=board_id)
                        all_docs.append(item_doc)
                except MiroNotFoundError:
                    logger.warning(
                        "miro.sync.board_not_found",
                        board_id=board_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "miro.sync.board_items_failed",
                        board_id=board_id,
                        error=str(exc),
                    )

            documents_found = len(all_docs)

            for doc in all_docs:
                try:
                    if _HAS_SDK:
                        normalized = NormalizedDocument(  # type: ignore[name-defined]
                            id=doc.id,
                            source_id=doc.metadata.get("board_id", doc.id),
                            title=doc.title,
                            content=doc.content,
                            content_type="text",
                            source_url=doc.metadata.get("view_link", ""),
                            author=doc.metadata.get("owner_name", ""),
                            source="miro",
                            tenant_id=self.tenant_id,
                            connector_id=self.connector_id,
                            metadata=doc.metadata,
                        )
                        await self.ingest_document(normalized, kb_id=kb_id or "")
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "miro.sync.doc_failed",
                        doc_id=doc.id,
                        error=str(exc),
                    )
                    documents_failed += 1

            status = (
                _LocalSyncStatus.COMPLETED
                if documents_failed == 0
                else _LocalSyncStatus.PARTIAL
            )
            msg = (
                f"Synced {documents_synced}/{documents_found} objects "
                f"({documents_failed} failed)"
            )

            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=(
                        SyncStatus.COMPLETED  # type: ignore[name-defined]
                        if documents_failed == 0
                        else SyncStatus.PARTIAL  # type: ignore[name-defined]
                    ),
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
                "miro.sync.failed",
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

    # ── list_boards ───────────────────────────────────────────────────────────

    async def list_boards(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """Return all boards accessible to the token, following cursor pagination.

        Keyword args are forwarded to get_boards() as extra query params
        (e.g. team_id= to filter by team).
        """
        boards: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            data = await with_retry(
                lambda c=cursor: self.client.get_boards(cursor=c, **kwargs),
                max_attempts=3,
            )
            page: List[Dict[str, Any]] = data.get("data", [])
            boards.extend(page)

            next_cursor: Optional[str] = data.get("cursor")
            if not next_cursor or not page:
                break
            cursor = next_cursor

        return boards

    # ── list_board_items ──────────────────────────────────────────────────────

    async def list_board_items(
        self,
        board_id: str,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Return all items on a board, following cursor pagination.

        Keyword args are forwarded to get_board_items() (e.g. type= to filter).
        """
        items: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            data = await with_retry(
                lambda c=cursor: self.client.get_board_items(
                    board_id, cursor=c, **kwargs
                ),
                max_attempts=3,
            )
            page: List[Dict[str, Any]] = data.get("data", [])
            items.extend(page)

            next_cursor: Optional[str] = data.get("cursor")
            if not next_cursor or not page:
                break
            cursor = next_cursor

        return items

    # ── get_board ─────────────────────────────────────────────────────────────

    async def get_board(self, board_id: str) -> Dict[str, Any]:
        """Retrieve a single Miro board by ID."""
        return await with_retry(
            lambda: self.client.get_board(board_id),
            max_attempts=3,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release held resources (no-op for stateless HTTP client)."""

    async def __aenter__(self) -> "MiroConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
