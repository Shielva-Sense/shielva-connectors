"""GitLab connector for the Shielva platform.

Provides authentication, health checks, full sync, and direct access to GitLab
projects, issues, merge requests, pipelines, users, and groups via the
GitLab REST API v4.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from client import GitLabHTTPClient
from exceptions import GitLabAuthError, GitLabError, GitLabNetworkError
from helpers import (
    CircuitBreaker,
    normalize_group,
    normalize_issue,
    normalize_merge_request,
    normalize_pipeline,
    normalize_project,
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

from shared.base_connector import BaseConnector

CONNECTOR_TYPE = "gitlab"
AUTH_TYPE = "api_key"

SYNC_PAGE_SIZE = 100
CIRCUIT_BREAKER_THRESHOLD = 5


class GitLabConnector(BaseConnector):
    """Shielva connector for GitLab.

    Supports gitlab.com and self-hosted instances via the base_url config field.
    Auth: PRIVATE-TOKEN header using Personal Access Token or Project Access Token.
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
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        # Spec field key is "api_key"; "access_token" accepted for backward-compat
        self._access_token: str = (
            _config.get("api_key", "") or _config.get("access_token", "")
        )
        self._base_url: str = _config.get("base_url", "https://gitlab.com")
        self._group_id: str = _config.get("group_id", "")
        self.client: GitLabHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> GitLabHTTPClient:
        return GitLabHTTPClient(config=self.config)

    def _ensure_client(self) -> GitLabHTTPClient:
        if self.client is None:
            self.client = self._make_client()
        return self.client

    def _has_credentials(self) -> bool:
        return bool(self._access_token)

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials — api_key must be present and accepted by GitLab."""
        if not self._has_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key (GitLab Personal Access Token) is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_current_user)
            await client.aclose()
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to GitLab",
            )
        except GitLabAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"GitLab authentication failed: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /user and return current health along with the authenticated username."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required",
            )
        client = self._make_client()
        try:
            user_data = await with_retry(client.get_current_user)
            await client.aclose()
            self._circuit_breaker.on_success()
            username: str = (
                user_data.get("username", "") or user_data.get("name", "")
                if isinstance(user_data, dict) else ""
            )
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"GitLab API is reachable. Authenticated as {username}",
                username=username,
            )
        except GitLabAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except GitLabNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = (
                ConnectorHealth.DEGRADED
                if not self._circuit_breaker.is_open
                else ConnectorHealth.OFFLINE
            )
            return HealthCheckResult(
                health=health,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync GitLab projects, issues, merge requests, pipelines, and groups.

        Fetches all accessible projects then for each project fetches its
        issues, merge requests, and pipelines. Groups are fetched globally.
        """
        _ = full, since  # incremental requires GitLab events API; deferred
        if self.client is None:
            self.client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # 1. Projects
        try:
            projects = await self._fetch_all_projects()
        except GitLabError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message=str(exc),
            )

        found += len(projects)
        for project in projects:
            try:
                doc = normalize_project(project, self.connector_id, self.tenant_id)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # 2. Per-project: issues, MRs, pipelines
        for project in projects:
            pid: int = project.get("id", 0)
            if not pid:
                continue

            # Issues
            try:
                issues = await with_retry(self.client.get_issues, pid)
                found += len(issues)
                for issue in issues:
                    try:
                        doc = normalize_issue(issue, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except GitLabError:
                pass

            # Merge Requests
            try:
                mrs = await with_retry(self.client.get_merge_requests, pid)
                found += len(mrs)
                for mr in mrs:
                    try:
                        doc = normalize_merge_request(mr, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except GitLabError:
                pass

            # Pipelines
            try:
                pipelines = await with_retry(self.client.get_pipelines, pid)
                found += len(pipelines)
                for pipeline in pipelines:
                    try:
                        doc = normalize_pipeline(pipeline, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except GitLabError:
                pass

        # 3. Groups (global)
        try:
            groups = await with_retry(self.client.get_groups)
            found += len(groups)
            for group in groups:
                try:
                    doc = normalize_group(group, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except GitLabError:
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_projects(self) -> list[dict[str, Any]]:
        assert self.client is not None
        return await with_retry(self.client.get_projects, SYNC_PAGE_SIZE)

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Direct resource accessors ─────────────────────────────────────────────

    async def list_projects(self, **kwargs: Any) -> list[dict[str, Any]]:
        """List all accessible projects."""
        client = self._ensure_client()
        return await with_retry(client.get_projects)

    async def list_issues(
        self,
        project_id: str | int | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """List issues — project-scoped when project_id is given, global otherwise."""
        client = self._ensure_client()
        return await with_retry(client.get_issues, project_id)

    async def list_merge_requests(
        self,
        project_id: str | int | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """List merge requests — project-scoped when project_id is given, global otherwise."""
        client = self._ensure_client()
        return await with_retry(client.get_merge_requests, project_id)

    async def list_pipelines(self, project_id: str | int) -> list[dict[str, Any]]:
        """List pipelines for a specific project."""
        client = self._ensure_client()
        return await with_retry(client.get_pipelines, project_id)

    async def list_groups(self) -> list[dict[str, Any]]:
        """List all accessible groups."""
        client = self._ensure_client()
        return await with_retry(client.get_groups)

    async def list_members(self, group_id: str | int | None = None) -> list[dict[str, Any]]:
        """List members of a group — uses config group_id when group_id param is not given."""
        effective_group = group_id or self._group_id
        if not effective_group:
            return []
        client = self._ensure_client()
        return await with_retry(client.list_members, effective_group, SYNC_PAGE_SIZE)

    async def get_project(self, project_id: str | int) -> dict[str, Any]:
        """Get a single project by ID or URL-encoded path."""
        client = self._ensure_client()
        return await with_retry(client.get_project, project_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def __aenter__(self) -> GitLabConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
