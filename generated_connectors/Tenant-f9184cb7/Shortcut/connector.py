"""Shortcut connector — orchestration only.

All HTTP calls -> ``client/http_client.py``.
All normalization -> ``helpers/normalizer.py``.
All utilities -> ``helpers/utils.py``.

Auth: API token sent via the ``Shortcut-Token`` request header. Shortcut
(formerly Clubhouse) REST API base: ``https://api.app.shortcut.com/api/v3``.

connector.py orchestrates only — HTTP is owned by ``client/http_client.py``,
data shaping by ``helpers/normalizer.py``. Multi-tenant: every
``NormalizedDocument`` id is ``f"{self.tenant_id}_{source_id}"``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import SHORTCUT_BASE_URL, ShortcutHTTPClient
from exceptions import (
    ShortcutAuthError,
    ShortcutError,
    ShortcutNetworkError,
    ShortcutNotFoundError,
    ShortcutRateLimitError,
)
from helpers.normalizer import normalize_epic, normalize_story
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)


class ShortcutConnector(BaseConnector):
    """Shielva connector for the Shortcut (formerly Clubhouse) REST API."""

    CONNECTOR_TYPE: str = "shortcut"
    CONNECTOR_NAME: str = "Shortcut"
    AUTH_TYPE: str = "api_key"

    # Required config keys for install() — all must be present and non-empty.
    REQUIRED_CONFIG_KEYS: List[str] = ["api_token"]

    # OCP — HTTP status -> (ConnectorHealth, AuthStatus) classification.
    # Used by ``_classify_failure`` so the lifecycle methods do not branch on
    # status codes inline.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        # ALWAYS read credentials from self.config — NEVER from os.environ.
        self.api_token: str = self.config.get("api_token", "") or ""
        self.base_url: str = (
            self.config.get("base_url", SHORTCUT_BASE_URL) or SHORTCUT_BASE_URL
        )
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 200)
        self.default_workflow_state_id: Optional[int] = self.config.get(
            "default_workflow_state_id"
        )

        # HTTP client constructed eagerly so tests can patch
        # ``connector.ShortcutHTTPClient`` BEFORE construction and the patched
        # instance is captured into ``self.client``.
        self.client: ShortcutHTTPClient = ShortcutHTTPClient(
            api_token=self.api_token,
            base_url=self.base_url,
        )

    # ── BaseConnector lifecycle ────────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate required config keys; persist them via ``save_config``.

        Per CONNECTOR_SYSTEM_PROMPT: ``install()`` MUST NOT call the API. The
        gateway calls ``health_check`` separately.
        """
        missing = [k for k in self.REQUIRED_CONFIG_KEYS if not self.config.get(k)]
        if missing:
            logger.warning(
                "shortcut.install.missing_credentials",
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
                missing=missing,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message=f"Missing: {', '.join(missing)}",
            )

        await self.save_config(
            {
                "api_token": self.api_token,
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
                "default_workflow_state_id": self.default_workflow_state_id,
            }
        )
        logger.info(
            "shortcut.install.ok",
            tenant_id=self.tenant_id,
            connector_id=self.connector_id,
            base_url=self.base_url,
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            connector_type=self.CONNECTOR_TYPE,
            message="Shortcut connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-token connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        ``TokenInfo`` whose ``access_token`` is the configured ``api_token``.
        """
        return TokenInfo(
            access_token=self.api_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Probe ``GET /member`` to verify the token + reachability."""
        if not self.api_token:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message="api_token not configured",
            )
        try:
            await self.client.get_current_member()
        except Exception as exc:  # caught at lifecycle boundary
            return self._classify_failure(exc)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_type=self.CONNECTOR_TYPE,
            message="Shortcut API reachable",
        )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Sync Shortcut stories + epics into the Shielva KB.

        Walks ``POST /search/stories`` cursor pages and ``GET /epics``;
        normalises each row; ingests via ``BaseConnector.ingest_document``.
        """
        started_at = datetime.now(timezone.utc)
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        if not self.api_token:
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                message="missing api_token",
            )

        try:
            # Stories — cursor-paginated search.
            query_parts: List[str] = []
            if since is not None:
                query_parts.append(f"updated:>{since.date().isoformat()}")
            query: Optional[str] = " ".join(query_parts) or None
            next_token: Optional[str] = None

            while True:
                page = await with_retry(
                    lambda nt=next_token: self.client.search_stories(
                        query=query, page_size=25, next_token=nt,
                    ),
                    max_retries=3,
                )
                stories = page.get("data") or []
                documents_found += len(stories)
                for story in stories:
                    try:
                        doc: NormalizedDocument = normalize_story(
                            story, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "shortcut.sync.story_failed",
                            story_id=story.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1

                next_token = page.get("next")
                if not next_token:
                    break

            # Epics — single GET (Shortcut returns all epics).
            epics = await with_retry(
                lambda: self.client.list_epics(includes_description=True),
                max_retries=3,
            )
            for epic in epics or []:
                documents_found += 1
                try:
                    doc = normalize_epic(epic, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "shortcut.sync.epic_failed",
                        epic_id=epic.get("id"),
                        error=str(exc),
                    )
                    documents_failed += 1

            return SyncResult(
                status=(
                    SyncStatus.SUCCESS
                    if documents_failed == 0
                    else SyncStatus.PARTIAL
                ),
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                message=f"Synced {documents_synced}/{documents_found} Shortcut documents",
            )
        except Exception as exc:
            logger.error(
                "shortcut.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                errors=[str(exc)],
                message=str(exc),
            )

    # ── Stories ────────────────────────────────────────────────────────────

    async def list_stories(
        self,
        query: Optional[str] = None,
        page_size: int = 25,
        next_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /search/stories — search stories with optional cursor pagination."""
        return await self.client.search_stories(
            query=query, page_size=page_size, next_token=next_token,
        )

    async def get_story(self, story_id: int) -> Dict[str, Any]:
        """GET /stories/{id}."""
        return await self.client.get_story(story_id)

    async def create_story(
        self,
        name: str,
        story_type: str = "feature",
        project_id: Optional[int] = None,
        workflow_state_id: Optional[int] = None,
        owner_ids: Optional[List[str]] = None,
        description: Optional[str] = None,
        epic_id: Optional[int] = None,
        iteration_id: Optional[int] = None,
        estimate: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /stories — create a new story.

        Falls back to ``default_workflow_state_id`` (from install config) when
        ``workflow_state_id`` is omitted, so the connector is usable without
        per-call wiring.
        """
        if not name:
            raise ValueError("create_story: 'name' is required")

        payload: Dict[str, Any] = {"name": name, "story_type": story_type}

        effective_workflow_state = workflow_state_id
        if (
            effective_workflow_state is None
            and self.default_workflow_state_id is not None
        ):
            effective_workflow_state = self.default_workflow_state_id

        if project_id is not None:
            payload["project_id"] = project_id
        if effective_workflow_state is not None:
            payload["workflow_state_id"] = effective_workflow_state
        if owner_ids:
            payload["owner_ids"] = list(owner_ids)
        if description is not None:
            payload["description"] = description
        if epic_id is not None:
            payload["epic_id"] = epic_id
        if iteration_id is not None:
            payload["iteration_id"] = iteration_id
        if estimate is not None:
            payload["estimate"] = estimate

        return await self.client.create_story(payload)

    async def update_story(
        self,
        story_id: int,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /stories/{id} — patch mutable fields on a story."""
        if not fields:
            raise ValueError("update_story: 'fields' must not be empty")
        return await self.client.update_story(story_id, fields)

    async def delete_story(self, story_id: int) -> Dict[str, Any]:
        """DELETE /stories/{id} — permanently delete a story."""
        return await self.client.delete_story(story_id)

    # ── Epics ──────────────────────────────────────────────────────────────

    async def list_epics(
        self,
        includes_description: bool = False,
    ) -> List[Dict[str, Any]]:
        """GET /epics — list all epics."""
        return await self.client.list_epics(includes_description=includes_description)

    async def get_epic(self, epic_id: int) -> Dict[str, Any]:
        """GET /epics/{id}."""
        return await self.client.get_epic(epic_id)

    async def create_epic(
        self,
        name: str,
        description: Optional[str] = None,
        state: str = "to do",
    ) -> Dict[str, Any]:
        """POST /epics — create a new epic."""
        if not name:
            raise ValueError("create_epic: 'name' is required")
        return await self.client.create_epic(
            name=name, description=description, state=state,
        )

    # ── Iterations / Milestones / Projects / Workflows ─────────────────────

    async def list_iterations(self) -> List[Dict[str, Any]]:
        """GET /iterations — list all iterations."""
        return await self.client.list_iterations()

    async def list_milestones(self) -> List[Dict[str, Any]]:
        """GET /milestones — list all milestones."""
        return await self.client.list_milestones()

    async def list_projects(self) -> List[Dict[str, Any]]:
        """GET /projects — list all (legacy) projects."""
        return await self.client.list_projects()

    async def list_workflows(self) -> List[Dict[str, Any]]:
        """GET /workflows — list workflows and their states."""
        return await self.client.list_workflows()

    # ── Members / Groups ───────────────────────────────────────────────────

    async def list_members(self) -> List[Dict[str, Any]]:
        """GET /members — list workspace members."""
        return await self.client.list_members()

    async def get_member(
        self, member_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /members/{id} (or /member when ``member_id`` is None)."""
        if not member_id:
            return await self.client.get_current_member()
        return await self.client.get_member(member_id)

    async def list_groups(self) -> List[Dict[str, Any]]:
        """GET /groups — list teams (groups)."""
        return await self.client.list_groups()

    # ── Labels / Files ─────────────────────────────────────────────────────

    async def list_labels(self) -> List[Dict[str, Any]]:
        """GET /labels — list workspace labels."""
        return await self.client.list_labels()

    async def create_label(
        self,
        name: str,
        color: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /labels — create a workspace label."""
        if not name:
            raise ValueError("create_label: 'name' is required")
        return await self.client.create_label(name=name, color=color)

    async def list_files(self) -> List[Dict[str, Any]]:
        """GET /files — list uploaded files metadata."""
        return await self.client.list_files()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _classify_failure(self, exc: Exception) -> ConnectorStatus:
        """Map exception -> ConnectorStatus following CONNECTOR_SYSTEM_PROMPT rule 8."""
        msg = str(exc)
        status_code: Optional[int] = getattr(exc, "status_code", None)

        if status_code == 403:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message=msg,
            )
        if isinstance(exc, ShortcutAuthError) or status_code == 401:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                connector_type=self.CONNECTOR_TYPE,
                message=msg,
            )
        if isinstance(exc, ShortcutRateLimitError) or status_code == 429:
            logger.warning(
                "shortcut.rate_limited",
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message="rate limited",
            )
        if isinstance(exc, ShortcutNetworkError):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message=msg,
            )
        if isinstance(exc, ShortcutNotFoundError) or status_code == 404:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.FAILED,
                connector_type=self.CONNECTOR_TYPE,
                message=msg,
            )
        if isinstance(exc, ShortcutError):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.FAILED,
                connector_type=self.CONNECTOR_TYPE,
                message=msg,
            )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.UNHEALTHY,
            auth_status=AuthStatus.FAILED,
            connector_type=self.CONNECTOR_TYPE,
            message=msg,
        )
