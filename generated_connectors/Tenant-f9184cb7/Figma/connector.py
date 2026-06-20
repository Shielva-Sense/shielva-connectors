"""Figma connector — orchestration only.

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
        def __init__(self, tenant_id: str = "", connector_id: str = "", config: Optional[Dict[str, Any]] = None) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

from client.http_client import FigmaHTTPClient
from exceptions import FigmaAuthError, FigmaError, FigmaNetworkError, FigmaNotFoundError
from helpers.utils import (
    normalize_comment,
    normalize_component,
    normalize_file,
    normalize_project,
    normalize_style,
    normalize_version,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    InstallResult,
    HealthCheckResult,
    SyncResult,
    SyncStatus,
)

logger = structlog.get_logger(__name__)

CONNECTOR_TYPE = "figma"
AUTH_TYPE = "api_key"

_FIGMA_API_BASE = "https://api.figma.com/v1"


class FigmaConnector(BaseConnector):
    """Shielva connector for Figma via the Figma REST API."""

    CONNECTOR_TYPE = "figma"
    CONNECTOR_NAME = "Figma"
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
        self.client = FigmaHTTPClient(config=self.config)

    def _get_api_key(self) -> str:
        """Return the Personal Access Token from config.

        Accepts both ``api_key`` (canonical) and ``personal_access_token``
        (legacy alias) to maintain backward compatibility.
        """
        return (
            self.config.get("api_key")
            or self.config.get("personal_access_token")
            or ""
        )

    def _get_team_id(self) -> str:
        """Return the optional team_id from config."""
        return self.config.get("team_id", "")

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that api_key is present in config."""
        api_key = self._get_api_key()

        if not api_key:
            logger.warning(
                "figma.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="api_key (Personal Access Token) is required",
            )

        logger.info("figma.install.ok", connector_id=self.connector_id)
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Connector installed — api_key (Personal Access Token) present",
        )

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Call GET /me to validate the api_key and retrieve authenticated user info."""
        try:
            data = await with_retry(
                lambda: self.client.get_me(),
                max_attempts=2,
            )
            handle = data.get("handle", data.get("id", "unknown"))
            email = data.get("email", "")
            msg = f"Connected — user: {handle}" + (f" ({email})" if email else "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
            )
        except FigmaAuthError as exc:
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

    async def sync(
        self,
        full: bool = False,
        since: Optional[Any] = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync Figma resources: projects → files → comments, plus components and styles.

        Requires ``team_id`` in config to access team-scoped resources.
        If no ``team_id`` is configured the sync completes with zero documents.
        """
        team_id = self._get_team_id()
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            all_docs: List[ConnectorDocument] = []

            if team_id:
                # Team projects → files per project → comments per file
                projects = await self.list_projects(team_id=team_id)
                for project_raw in projects:
                    doc = normalize_project(project_raw, team_id=team_id)
                    all_docs.append(doc)

                    project_id = str(project_raw.get("id", ""))
                    if project_id:
                        try:
                            files = await self.list_files(project_id=project_id)
                            for file_raw in files:
                                file_doc = normalize_file(file_raw, project_id=project_id)
                                all_docs.append(file_doc)

                                file_key = file_raw.get("key", "")
                                if file_key:
                                    try:
                                        comments = await self.get_file_comments(file_key=file_key)
                                        for comment_raw in comments:
                                            comment_doc = normalize_comment(comment_raw, file_key=file_key)
                                            all_docs.append(comment_doc)
                                    except FigmaNotFoundError:
                                        pass
                                    except Exception as exc:
                                        logger.warning(
                                            "figma.sync.comments_failed",
                                            file_key=file_key,
                                            error=str(exc),
                                        )
                        except Exception as exc:
                            logger.warning(
                                "figma.sync.project_files_failed",
                                project_id=project_id,
                                error=str(exc),
                            )

                # Team components
                try:
                    components = await self.list_components(team_id=team_id)
                    for comp_raw in components:
                        comp_doc = normalize_component(comp_raw, team_id=team_id)
                        all_docs.append(comp_doc)
                except Exception as exc:
                    logger.warning(
                        "figma.sync.components_failed",
                        team_id=team_id,
                        error=str(exc),
                    )

                # Team styles
                try:
                    styles = await self.list_styles(team_id=team_id)
                    for style_raw in styles:
                        style_doc = normalize_style(style_raw, team_id=team_id)
                        all_docs.append(style_doc)
                except Exception as exc:
                    logger.warning(
                        "figma.sync.styles_failed",
                        team_id=team_id,
                        error=str(exc),
                    )
            else:
                logger.info(
                    "figma.sync.no_team_id",
                    connector_id=self.connector_id,
                    message="No team_id configured — syncing without team context",
                )

            documents_found = len(all_docs)

            for doc in all_docs:
                try:
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "figma.sync.doc_failed",
                        doc_id=doc.id,
                        error=str(exc),
                    )
                    documents_synced -= 1
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
                "figma.sync.failed",
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

    # ── list_projects ─────────────────────────────────────────────────────────

    async def list_projects(self, team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return the list of projects for the given team_id.

        Uses the ``team_id`` from config if not explicitly provided.
        """
        tid = team_id or self._get_team_id()
        if not tid:
            return []
        data = await with_retry(
            lambda: self.client.list_projects(tid),
            max_attempts=3,
        )
        return data.get("projects", [])

    # ── list_files ────────────────────────────────────────────────────────────

    async def list_files(self, project_id: str) -> List[Dict[str, Any]]:
        """Return the list of files for the given project_id."""
        data = await with_retry(
            lambda: self.client.list_files(project_id),
            max_attempts=3,
        )
        return data.get("files", [])

    # ── get_file ──────────────────────────────────────────────────────────────

    async def get_file(self, file_key: str) -> Dict[str, Any]:
        """Retrieve the full document tree for a Figma file."""
        return await with_retry(
            lambda: self.client.get_file(file_key),
            max_attempts=3,
        )

    # ── get_file_comments ─────────────────────────────────────────────────────

    async def get_file_comments(self, file_key: str) -> List[Dict[str, Any]]:
        """Return all comments for the given file_key."""
        data = await with_retry(
            lambda: self.client.get_file_comments(file_key),
            max_attempts=3,
        )
        return data.get("comments", [])

    # Backward-compat alias used in sync
    async def list_comments(self, file_key: str) -> List[Dict[str, Any]]:
        """Alias for get_file_comments."""
        return await self.get_file_comments(file_key)

    # ── get_file_versions ─────────────────────────────────────────────────────

    async def get_file_versions(self, file_key: str) -> List[Dict[str, Any]]:
        """Return version history for the given file_key."""
        data = await with_retry(
            lambda: self.client.get_file_versions(file_key),
            max_attempts=3,
        )
        return data.get("versions", [])

    # ── list_components ───────────────────────────────────────────────────────

    async def list_components(self, team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return all published components for the given team (cursor-paginated).

        Uses the ``team_id`` from config if not explicitly provided.
        """
        tid = team_id or self._get_team_id()
        if not tid:
            return []

        components: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            data = await with_retry(
                lambda c=cursor: self.client.get_team_components(tid, cursor=c),
                max_attempts=3,
            )
            meta = data.get("meta", {})
            page_components: List[Dict[str, Any]] = meta.get("components", [])
            components.extend(page_components)

            next_cursor = meta.get("cursor")
            if not next_cursor or not page_components:
                break
            cursor = next_cursor

        return components

    # Backward-compat alias
    async def list_team_components(self, team_id: str) -> List[Dict[str, Any]]:
        """Alias for list_components — kept for backward compatibility."""
        return await self.list_components(team_id=team_id)

    # ── list_styles ───────────────────────────────────────────────────────────

    async def list_styles(self, team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return all published styles for the given team (cursor-paginated).

        Uses the ``team_id`` from config if not explicitly provided.
        """
        tid = team_id or self._get_team_id()
        if not tid:
            return []

        styles: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            data = await with_retry(
                lambda c=cursor: self.client.get_team_styles(tid, cursor=c),
                max_attempts=3,
            )
            meta = data.get("meta", {})
            page_styles: List[Dict[str, Any]] = meta.get("styles", [])
            styles.extend(page_styles)

            next_cursor = meta.get("cursor")
            if not next_cursor or not page_styles:
                break
            cursor = next_cursor

        return styles

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> "FigmaConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
