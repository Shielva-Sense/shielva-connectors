from __future__ import annotations

from typing import Any, Dict

from client import JiraHTTPClient
from exceptions import JiraAuthError, JiraError, JiraNetworkError
from helpers import normalize_issue, normalize_project, with_retry
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

CONNECTOR_TYPE: str = "jira"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 100
DEFAULT_ISSUE_FIELDS: str = (
    "summary,status,assignee,reporter,priority,created,updated,issuetype"
)


class JiraConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Jira.

    Provides authentication, health checks, full issue sync (with pagination),
    project listing, issue search, board and sprint listing, and user listing
    via the Atlassian Jira REST API v3 and Agile API v1.

    Authentication uses HTTP Basic Auth: ``email:api_token`` base64-encoded in
    the ``Authorization`` header.
    """

    CONNECTOR_TYPE: str = "jira"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(
            tenant_id=tenant_id,
            connector_id=connector_id,
            config=_config,
        )
        self._domain: str = _config.get("domain", "").strip()
        self._email: str = _config.get("email", "").strip()
        self._api_token: str = _config.get("api_token", "").strip()
        self.http_client: JiraHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> JiraHTTPClient:
        return JiraHTTPClient(
            domain=self._domain,
            email=self._email,
            api_token=self._api_token,
        )

    def _ensure_client(self) -> JiraHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    def _has_credentials(self) -> bool:
        return bool(self._domain and self._email and self._api_token)

    def _build_source_url(self, issue_key: str) -> str:
        if self._domain and issue_key:
            return f"https://{self._domain}/browse/{issue_key}"
        return ""

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate domain/email/api_token by calling GET /myself."""
        if not self._has_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="domain, email, and api_token are all required",
            )
        client = self._make_client()
        try:
            myself = await with_retry(client.get_myself)
            await client.aclose()
            display_name: str = (
                myself.get("displayName", "")
                or myself.get("emailAddress", "")
                or "Unknown user"
            )
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Jira as {display_name}",
            )
        except JiraAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Jira authentication failed: {exc}",
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
        """Ping GET /myself and return current health."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="domain, email, and api_token are required",
            )
        client = self._make_client()
        try:
            myself = await with_retry(client.get_myself)
            await client.aclose()
            display_name: str = myself.get("displayName", "") or "unknown"
            email_address: str = myself.get("emailAddress", "")
            details = f"user: {display_name}"
            if email_address:
                details += f" ({email_address})"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Jira API is reachable ({details})",
            )
        except JiraAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except JiraNetworkError as exc:
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

    async def sync(self, kb_id: str = "", **kwargs: Any) -> SyncResult:
        """Sync all Jira issues and projects.

        Lists all projects, then searches all issues via JQL with startAt
        pagination. Each object is normalized and optionally ingested via
        ``_ingest_document`` when ``kb_id`` is provided.
        """
        if not self._has_credentials():
            return SyncResult(
                status=SyncStatus.FAILED,
                message="domain, email, and api_token are required",
            )

        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0

        # Sync projects
        try:
            projects = await self._fetch_all_projects(client)
            found += len(projects)
            for project in projects:
                try:
                    doc = normalize_project(
                        project, self.connector_id, self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except JiraError:
            pass  # non-fatal — continue with issues

        # Sync issues
        try:
            issues = await self._fetch_all_issues(client)
            found += len(issues)
            for issue in issues:
                try:
                    doc = normalize_issue(
                        issue, self.connector_id, self.tenant_id
                    )
                    issue_key = issue.get("key", "")
                    if self._domain and issue_key:
                        doc.source_url = self._build_source_url(issue_key)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except JiraError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_projects(
        self, client: JiraHTTPClient
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        start_at = 0
        while True:
            page = await with_retry(
                client.list_projects,
                max_results=SYNC_PAGE_SIZE,
                start_at=start_at,
            )
            batch: list[dict[str, Any]] = page.get("values", [])
            records.extend(batch)
            total: int = page.get("total", 0)
            start_at += len(batch)
            if not batch or start_at >= total:
                break
        return records

    async def _fetch_all_issues(
        self,
        client: JiraHTTPClient,
        jql: str = "ORDER BY updated DESC",
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        start_at = 0
        while True:
            page = await with_retry(
                client.search_issues,
                jql,
                SYNC_PAGE_SIZE,
                start_at,
                DEFAULT_ISSUE_FIELDS,
            )
            batch: list[dict[str, Any]] = page.get("issues", [])
            records.extend(batch)
            total: int = page.get("total", 0)
            start_at += len(batch)
            if not batch or start_at >= total:
                break
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Projects ──────────────────────────────────────────────────────────────

    async def list_projects(
        self, max_results: int = 50
    ) -> list[dict[str, Any]]:
        """Return a list of project dicts from GET /project/search."""
        client = self._ensure_client()
        page = await with_retry(
            client.list_projects,
            max_results=max_results,
        )
        return page.get("values", [])

    # ── Issues ────────────────────────────────────────────────────────────────

    async def search_issues(
        self,
        jql: str = "",
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """Return a list of issue dicts from POST /search."""
        client = self._ensure_client()
        page = await with_retry(
            client.search_issues,
            jql,
            max_results,
            0,
            DEFAULT_ISSUE_FIELDS,
        )
        return page.get("issues", [])

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        """Return a single issue dict from GET /issue/{issue_key}."""
        client = self._ensure_client()
        return await with_retry(client.get_issue, issue_key)

    # ── Boards ────────────────────────────────────────────────────────────────

    async def list_boards(
        self, project_key: str | None = None
    ) -> list[dict[str, Any]]:
        """Return a list of board dicts from GET /rest/agile/1.0/board."""
        client = self._ensure_client()
        page = await with_retry(
            client.list_boards,
            project_key,
        )
        return page.get("values", [])

    # ── Sprints ───────────────────────────────────────────────────────────────

    async def list_sprints(self, board_id: int | str) -> list[dict[str, Any]]:
        """Return a list of sprint dicts from GET /rest/agile/1.0/board/{id}/sprint."""
        client = self._ensure_client()
        page = await with_retry(client.list_sprints, board_id)
        return page.get("values", [])

    # ── Users ─────────────────────────────────────────────────────────────────

    async def list_users(self) -> list[dict[str, Any]]:
        """Return a list of user dicts from GET /users/search."""
        client = self._ensure_client()
        return await with_retry(client.list_users)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> JiraConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
