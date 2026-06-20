from __future__ import annotations

from typing import Any, Dict

from client import LinearHTTPClient
from exceptions import LinearAuthError, LinearError, LinearNetworkError
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

try:
    from shared.base_connector import BaseConnector
    _BASE = BaseConnector
except ImportError:
    _BASE = object  # standalone / test mode

SYNC_PAGE_SIZE: int = 50
CONNECTOR_TYPE: str = "linear"
AUTH_TYPE: str = "api_key"

# ── GraphQL queries ───────────────────────────────────────────────────────────

_VIEWER_QUERY = """
query {
    viewer {
        name
        email
    }
}
"""

_TEAMS_QUERY = """
query {
    teams {
        nodes {
            id
            name
            key
        }
    }
}
"""

_ISSUES_QUERY = """
query ListIssues($filter: IssueFilter, $first: Int, $after: String) {
    issues(filter: $filter, first: $first, after: $after) {
        nodes {
            id
            title
            description
            state {
                name
            }
            priority
            assignee {
                name
            }
            team {
                name
                key
            }
            createdAt
            updatedAt
        }
        pageInfo {
            hasNextPage
            endCursor
        }
    }
}
"""

_ISSUE_QUERY = """
query GetIssue($id: String!) {
    issue(id: $id) {
        id
        title
        description
        state {
            name
        }
        priority
        assignee {
            name
        }
        team {
            name
            key
        }
        createdAt
        updatedAt
    }
}
"""

_PROJECTS_QUERY = """
query {
    projects {
        nodes {
            id
            name
            description
            state
        }
    }
}
"""


class LinearConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Linear.

    Syncs issues from all teams via the Linear GraphQL API using
    Personal API key (Bearer token) authentication.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
        # Convenience keyword arg for standalone / test usage
        api_key: str = "",
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        self._api_key: str = _config.get("api_key", "") or api_key
        self._http_client: LinearHTTPClient | None = None

    def _make_client(self) -> LinearHTTPClient:
        return LinearHTTPClient()

    def _ensure_client(self) -> LinearHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._api_key:
            missing.append("api_key")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install credentials via GraphQL viewer query."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.graphql_query,
                self._api_key,
                _VIEWER_QUERY,
            )
            viewer: dict[str, Any] = data.get("viewer", {}) or {}
            name: str = viewer.get("name", "")
            email: str = viewer.get("email", "")
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Linear as {name} ({email})",
            )
        except LinearAuthError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping the Linear GraphQL API via the viewer query."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.graphql_query,
                self._api_key,
                _VIEWER_QUERY,
            )
            viewer: dict[str, Any] = data.get("viewer", {}) or {}
            name: str = viewer.get("name", "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Linear API reachable. User: {name}",
            )
        except LinearAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except LinearNetworkError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: object | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """Sync all issues from all Linear teams into the knowledge base.

        Paginates through every team's issues using Linear's cursor-based
        pagination (pageInfo.hasNextPage + endCursor).
        """
        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0

        # Fetch all teams first
        try:
            teams_data = await with_retry(
                client.graphql_query,
                self._api_key,
                _TEAMS_QUERY,
            )
        except LinearError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        teams: list[dict[str, Any]] = (
            teams_data.get("teams", {}).get("nodes", []) or []
        )

        for team in teams:
            team_id: str = team.get("id", "")
            cursor: str | None = None

            while True:
                variables: dict[str, Any] = {
                    "filter": {"team": {"id": {"eq": team_id}}},
                    "first": SYNC_PAGE_SIZE,
                }
                if cursor:
                    variables["after"] = cursor

                try:
                    page_data = await with_retry(
                        client.graphql_query,
                        self._api_key,
                        _ISSUES_QUERY,
                        variables,
                    )
                except LinearError as exc:
                    return SyncResult(
                        status=SyncStatus.FAILED,
                        documents_found=found,
                        documents_synced=synced,
                        documents_failed=failed,
                        message=str(exc),
                    )

                issues_block: dict[str, Any] = page_data.get("issues", {}) or {}
                nodes: list[dict[str, Any]] = issues_block.get("nodes", []) or []
                page_info: dict[str, Any] = issues_block.get("pageInfo", {}) or {}

                found += len(nodes)

                for issue in nodes:
                    try:
                        doc = normalize_issue(
                            issue,
                            self.connector_id,
                            self._tenant_id,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")

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

    # ── Teams ─────────────────────────────────────────────────────────────────

    async def list_teams(self) -> dict[str, Any]:
        """Return all Linear teams: id, name, key."""
        client = self._ensure_client()
        data = await with_retry(
            client.graphql_query,
            self._api_key,
            _TEAMS_QUERY,
        )
        return data.get("teams", {})

    # ── Issues ────────────────────────────────────────────────────────────────

    async def list_issues(
        self,
        team_id: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Return a page of Linear issues, optionally filtered by team_id.

        Uses cursor-based pagination. Pass ``after=pageInfo.endCursor`` for
        the next page.
        """
        client = self._ensure_client()
        variables: dict[str, Any] = {"first": limit}
        if team_id:
            variables["filter"] = {"team": {"id": {"eq": team_id}}}
        if after:
            variables["after"] = after
        data = await with_retry(
            client.graphql_query,
            self._api_key,
            _ISSUES_QUERY,
            variables,
        )
        return data.get("issues", {})

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        """Return a single Linear issue by its ID."""
        client = self._ensure_client()
        data = await with_retry(
            client.graphql_query,
            self._api_key,
            _ISSUE_QUERY,
            {"id": issue_id},
        )
        issue: dict[str, Any] | None = data.get("issue")
        if issue is None:
            from exceptions import LinearNotFoundError
            raise LinearNotFoundError("issue", issue_id)
        return issue

    # ── Projects ──────────────────────────────────────────────────────────────

    async def list_projects(self) -> dict[str, Any]:
        """Return all Linear projects: id, name, description, state."""
        client = self._ensure_client()
        data = await with_retry(
            client.graphql_query,
            self._api_key,
            _PROJECTS_QUERY,
        )
        return data.get("projects", {})

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> LinearConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
