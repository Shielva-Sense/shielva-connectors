"""Loom connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from shared.base_connector import BaseConnector

CONNECTOR_TYPE = "loom"
AUTH_TYPE = "api_key"

logger = structlog.get_logger(__name__)

from client.http_client import LoomHTTPClient
from exceptions import LoomAuthError, LoomError, LoomNetworkError, LoomNotFoundError
from helpers.utils import normalize_video, normalize_folder, normalize_workspace, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)


class LoomConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Loom via the Loom API v1.

    Syncs videos, folders, and workspaces. Authentication uses a
    Bearer API key sent as ``Authorization: Bearer {api_key}``.
    """

    CONNECTOR_TYPE = "loom"
    CONNECTOR_NAME = "Loom"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS = ["api_key"]

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        try:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=cfg)
        except TypeError:
            # BaseConnector shim — call with positional args
            super().__init__(tenant_id, connector_id, cfg)
        self.client = LoomHTTPClient(config=self.config)

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that api_key is present in the connector config.

        Does not make any API calls — credential presence check only.
        A subsequent health_check() call will verify the key is valid.
        """
        api_key = self.config.get("api_key")

        if not api_key:
            logger.warning(
                "loom.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="api_key is required",
            )

        logger.info("loom.install.ok", connector_id=self.connector_id)
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Connector installed — API key present",
        )

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Call GET /me to validate the API key and return workspace info."""
        try:
            data = await with_retry(
                lambda: self.client.get_me(),
                max_attempts=2,
            )
            # Loom /me returns user name / email / workspace info
            name = data.get("name", data.get("email", data.get("id", "unknown")))
            msg = f"Connected — user: {name}"

            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
            )
        except LoomAuthError as exc:
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
        """Sync all Loom videos, folders, and workspaces.

        Paginates through all videos using the ``next_page`` cursor.
        For each video, attempts to fetch the transcript for richer content.
        Also collects folders and workspaces.

        Returns a SyncResult with counts of found/synced/failed documents.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # 1. Sync videos (main resource — paginated)
            videos = await self._paginate_videos()
            documents_found += len(videos)

            for raw_video in videos:
                video_id = raw_video.get("id", "")
                try:
                    # Attempt to fetch transcript (best-effort)
                    transcript_text: Optional[str] = None
                    try:
                        t_data = await with_retry(
                            lambda vid=video_id: self.client.get_video_transcript(vid),
                            max_attempts=2,
                        )
                        transcript_text = t_data.get("transcript", "")
                    except (LoomNotFoundError, LoomError):
                        pass

                    doc = normalize_video(
                        raw_video,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        transcript=transcript_text,
                    )
                    documents_synced += 1
                    logger.debug(
                        "loom.sync.video_synced",
                        video_id=video_id,
                        doc_id=doc.id,
                    )
                except Exception as exc:
                    logger.error(
                        "loom.sync.video_failed",
                        video_id=video_id,
                        error=str(exc),
                    )
                    documents_failed += 1

            # 2. Sync folders (best-effort)
            try:
                folders = await self.list_folders()
                documents_found += len(folders)
                for raw_folder in folders:
                    try:
                        normalize_folder(
                            raw_folder,
                            connector_id=self.connector_id,
                            tenant_id=self.tenant_id,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("loom.sync.folder_failed", error=str(exc))
                        documents_failed += 1
            except Exception as exc:
                logger.warning("loom.sync.folders_skipped", error=str(exc))

            # 3. Sync workspaces (best-effort)
            try:
                workspaces = await self.list_workspaces()
                documents_found += len(workspaces)
                for raw_ws in workspaces:
                    try:
                        normalize_workspace(
                            raw_ws,
                            connector_id=self.connector_id,
                            tenant_id=self.tenant_id,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("loom.sync.workspace_failed", error=str(exc))
                        documents_failed += 1
            except Exception as exc:
                logger.warning("loom.sync.workspaces_skipped", error=str(exc))

            status = SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            msg = (
                f"Synced {documents_synced}/{documents_found} resources "
                f"({documents_failed} failed)"
            )
            logger.info(
                "loom.sync.completed",
                found=documents_found,
                synced=documents_synced,
                failed=documents_failed,
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=msg,
            )

        except Exception as exc:
            logger.error("loom.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── list_videos ───────────────────────────────────────────────────────────

    async def list_videos(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """Return all videos accessible to the API key (paginated).

        Follows ``next_page`` cursor until exhausted.
        """
        return await self._paginate_videos()

    async def _paginate_videos(self) -> List[Dict[str, Any]]:
        """Internal: paginate GET /videos using next_page cursor."""
        all_videos: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda c=cursor: self.client.get_videos(next_page=c),
                max_attempts=3,
            )
            videos = resp.get("videos", resp.get("data", []))
            all_videos.extend(videos)
            cursor = resp.get("next_page")
            if not cursor:
                break

        return all_videos

    # ── list_folders ──────────────────────────────────────────────────────────

    async def list_folders(self, folder_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return folders accessible to the API key.

        If folder_id is provided, returns that folder's details wrapped in a list.
        Otherwise returns the root folder listing.

        Args:
            folder_id: optional folder ID to fetch a specific folder.
        """
        resp = await with_retry(
            lambda: self.client.get_folders(folder_id=folder_id),
            max_attempts=3,
        )
        # API may return a list directly, or a dict with a `folders` / `data` key
        if isinstance(resp, list):
            return resp
        if folder_id:
            # Single folder response — wrap in list
            return [resp]
        return resp.get("folders", resp.get("data", [resp]))

    # ── list_workspaces ───────────────────────────────────────────────────────

    async def list_workspaces(self) -> List[Dict[str, Any]]:
        """Return all workspaces accessible to the API key."""
        resp = await with_retry(
            lambda: self.client.get_workspaces(),
            max_attempts=3,
        )
        if isinstance(resp, list):
            return resp
        return resp.get("workspaces", resp.get("data", [resp]))

    # ── get_video ─────────────────────────────────────────────────────────────

    async def get_video(self, video_id: str) -> Dict[str, Any]:
        """Retrieve a single Loom video by ID.

        Args:
            video_id: the Loom video identifier.
        """
        return await with_retry(
            lambda: self.client.get_video(video_id),
            max_attempts=3,
        )

    # ── get_video_transcript ──────────────────────────────────────────────────

    async def get_video_transcript(self, video_id: str) -> Dict[str, Any]:
        """Retrieve the transcript for a Loom video.

        Args:
            video_id: the Loom video identifier.

        Returns:
            dict containing the `transcript` text (empty if unavailable).
        """
        return await with_retry(
            lambda: self.client.get_video_transcript(video_id),
            max_attempts=3,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release any held resources (HTTP sessions are per-request)."""
        pass

    async def __aenter__(self) -> "LoomConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
