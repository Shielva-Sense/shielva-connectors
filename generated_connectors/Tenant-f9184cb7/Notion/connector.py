"""Notion connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

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

from client.http_client import NotionHTTPClient
from exceptions import NotionAuthError, NotionError, NotionNetworkError, NotionNotFoundError
from helpers.utils import normalize_page, normalize_database, with_retry
from models import (
    AuthStatus,
    ConnectorHealth,
    ConnectorDocument,
    InstallResult,
    HealthCheckResult,
    SyncResult,
    SyncStatus,
)

logger = structlog.get_logger(__name__)

_NOTION_API_BASE = "https://api.notion.com/v1"


class NotionConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Notion via the Notion API v1."""

    CONNECTOR_TYPE = "notion"
    CONNECTOR_NAME = "Notion"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS = ["api_key"]

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        super().__init__(tenant_id, connector_id, cfg)
        self._http_client: Optional[NotionHTTPClient] = None

    def _ensure_client(self) -> NotionHTTPClient:
        if self._http_client is None:
            self._http_client = NotionHTTPClient(base_url=_NOTION_API_BASE)
        return self._http_client

    def _get_token(self) -> str:
        # Primary key is api_key per spec; fall back to integration_token for compat
        return self.config.get("api_key", "") or self.config.get("integration_token", "")

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that api_key is present in config."""
        api_key = self._get_token()

        if not api_key:
            logger.warning(
                "notion.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="api_key is required",
            )

        logger.info("notion.install.ok", connector_id=self.connector_id)
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Connector installed — integration token present",
        )

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Call GET /users/me to validate the token and retrieve bot name."""
        token = self._get_token()
        try:
            data = await with_retry(
                lambda: self._ensure_client().get_bot_user(token),
                max_attempts=2,
            )
            bot_name = data.get("name", "")
            if not bot_name:
                bot_name = data.get("id", "unknown bot")
            msg = f"Connected — bot: {bot_name}"

            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
            )
        except NotionAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── sync ─────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Search all accessible pages and databases, normalize, and return SyncResult."""
        token = self._get_token()
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            results = await self._paginate_search(token, query="", filter_type=None)
            documents_found = len(results)

            for obj in results:
                obj_type = obj.get("object", "")
                obj_id = obj.get("id", "")

                try:
                    if obj_type == "page":
                        blocks: List[Dict[str, Any]] = []
                        try:
                            blocks = await self._paginate_blocks(token, obj_id)
                        except Exception:
                            pass

                        doc = normalize_page(
                            obj,
                            self.connector_id,
                            self.tenant_id,
                            content_blocks=blocks,
                        )
                    elif obj_type == "database":
                        doc = normalize_database(
                            obj,
                            self.connector_id,
                            self.tenant_id,
                        )
                    else:
                        documents_found -= 1
                        continue

                    documents_synced += 1

                except Exception as exc:
                    logger.error(
                        "notion.sync.object_failed",
                        object_id=obj_id,
                        object_type=obj_type,
                        error=str(exc),
                    )
                    documents_failed += 1

            status = SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            msg = (
                f"Synced {documents_synced}/{documents_found} objects "
                f"({documents_failed} failed)"
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=msg,
            )

        except Exception as exc:
            logger.error(
                "notion.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── list_pages ────────────────────────────────────────────────────────────

    async def list_pages(
        self,
        query: str = "",
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search for all accessible pages, returning a list of page dicts."""
        token = self._get_token()
        results: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda c=cursor: self._ensure_client().search(
                    token,
                    query=query,
                    filter_type="page",
                    start_cursor=c,
                    page_size=page_size,
                ),
                max_attempts=3,
            )
            results.extend(resp.get("results", []))
            if not resp.get("has_more", False):
                break
            cursor = resp.get("next_cursor")
            if not cursor:
                break

        return results

    # ── list_databases ────────────────────────────────────────────────────────

    async def list_databases(self, page_size: int = 100) -> List[Dict[str, Any]]:
        """List all accessible databases, returning a list of database dicts."""
        token = self._get_token()
        results: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda c=cursor: self._ensure_client().search(
                    token,
                    query="",
                    filter_type="database",
                    start_cursor=c,
                    page_size=page_size,
                ),
                max_attempts=3,
            )
            results.extend(resp.get("results", []))
            if not resp.get("has_more", False):
                break
            cursor = resp.get("next_cursor")
            if not cursor:
                break

        return results

    # ── get_page ─────────────────────────────────────────────────────────────

    async def get_page(self, page_id: str) -> Dict[str, Any]:
        """GET /pages/{page_id} — retrieve a single Notion page."""
        token = self._get_token()
        return await with_retry(
            lambda: self._ensure_client().get_page(token, page_id),
            max_attempts=3,
        )

    # ── get_database ──────────────────────────────────────────────────────────

    async def get_database(self, database_id: str) -> Dict[str, Any]:
        """GET /databases/{database_id} — retrieve a single database schema."""
        token = self._get_token()
        return await with_retry(
            lambda: self._ensure_client().get_database(token, database_id),
            max_attempts=3,
        )

    # ── query_database ────────────────────────────────────────────────────────

    async def query_database(
        self,
        database_id: str,
        filter: Optional[Dict[str, Any]] = None,
        sorts: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """POST /databases/{database_id}/query — query database rows (pages)."""
        token = self._get_token()
        results: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda c=cursor: self._ensure_client().query_database(
                    token,
                    database_id,
                    filter_obj=filter,
                    sorts=sorts,
                    start_cursor=c,
                ),
                max_attempts=3,
            )
            results.extend(resp.get("results", []))
            if not resp.get("has_more", False):
                break
            cursor = resp.get("next_cursor")
            if not cursor:
                break

        return results

    # ── get_page_blocks ───────────────────────────────────────────────────────

    async def get_page_blocks(
        self,
        page_id: str,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """GET /blocks/{page_id}/children — fetch all block content for a page.

        Returns a flat list of block objects including nested children.
        """
        token = self._get_token()
        return await self._paginate_blocks(token, page_id, recursive=True, page_size=page_size)

    # Backward-compat alias
    async def get_page_content(self, page_id: str) -> List[Dict[str, Any]]:
        """Alias for get_page_blocks — kept for backward compatibility."""
        return await self.get_page_blocks(page_id)

    # ── search ────────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str = "",
        filter_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """POST /search — return all pages and/or databases matching the query."""
        token = self._get_token()
        return await self._paginate_search(token, query=query, filter_type=filter_type)

    async def _paginate_search(
        self,
        token: str,
        query: str = "",
        filter_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda c=cursor: self._ensure_client().search(
                    token,
                    query=query,
                    filter_type=filter_type,
                    start_cursor=c,
                ),
                max_attempts=3,
            )
            results.extend(resp.get("results", []))
            if not resp.get("has_more", False):
                break
            cursor = resp.get("next_cursor")
            if not cursor:
                break

        return results

    async def _paginate_blocks(
        self,
        token: str,
        block_id: str,
        recursive: bool = True,
        page_size: int = 100,
    ) -> List[Dict[str, Any]]:
        all_blocks: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda c=cursor: self._ensure_client().get_block_children(
                    token, block_id, start_cursor=c, page_size=page_size
                ),
                max_attempts=3,
            )
            blocks = resp.get("results", [])
            all_blocks.extend(blocks)

            if recursive:
                for block in blocks:
                    if block.get("has_children", False):
                        child_id = block.get("id", "")
                        if child_id:
                            try:
                                children = await self._paginate_blocks(
                                    token, child_id, recursive=True
                                )
                                all_blocks.extend(children)
                            except Exception:
                                pass

            if not resp.get("has_more", False):
                break
            cursor = resp.get("next_cursor")
            if not cursor:
                break

        return all_blocks

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> "NotionConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
