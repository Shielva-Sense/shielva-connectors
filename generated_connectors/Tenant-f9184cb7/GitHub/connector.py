from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import GitHubHTTPClient
from exceptions import GitHubAuthError, GitHubError, GitHubNetworkError
from helpers import (
    CircuitBreaker,
    normalize_issue,
    normalize_pr,
    normalize_repo,
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

SYNC_PAGE_SIZE = 100
CIRCUIT_BREAKER_THRESHOLD = 5


class GitHubConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for GitHub.

    Provides authentication, health checks, full sync, and direct access to
    GitHub repositories, issues, and pull requests via the GitHub REST API v3.
    """

    CONNECTOR_TYPE: str = "github"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        # GitHub-specific attrs — field key is "api_key" per connector spec
        self._access_token: str = _config.get("api_key", "") or _config.get("access_token", "")
        self._org: str = _config.get("org", "")
        self.http_client: GitHubHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> GitHubHTTPClient:
        return GitHubHTTPClient(access_token=self._access_token)

    def _has_credentials(self) -> bool:
        return bool(self._access_token)

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials — access_token must be present."""
        if not self._has_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token (GitHub Personal Access Token) is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_authenticated_user)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to GitHub",
            )
        except GitHubAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"GitHub authentication failed: {exc}",
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
            user_data = await with_retry(client.get_authenticated_user)
            await client.aclose()
            self._circuit_breaker.on_success()
            username: str = user_data.get("login", "") if isinstance(user_data, dict) else ""
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"GitHub API is reachable. Authenticated as {username}",
                username=username,
            )
        except GitHubAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except GitHubNetworkError as exc:
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
    ) -> SyncResult:
        """
        Sync GitHub repositories, issues, and pull requests into the knowledge base.

        Fetches all repos (user-level or org-level when org is configured), then
        for each repo fetches open issues and open pull requests.
        """
        _ = since  # GitHub list endpoints are cursor-paginated; incremental needs events API
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # 1. Fetch repos
        try:
            repos = await self._fetch_all_repos()
        except GitHubError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message=str(exc),
            )

        found += len(repos)
        for repo in repos:
            try:
                doc = normalize_repo(repo, self.connector_id, self.tenant_id)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # 2. For each repo, fetch issues and PRs
        for repo in repos:
            owner_login: str = (repo.get("owner") or {}).get("login", "") or ""
            repo_name: str = repo.get("name", "") or ""
            if not owner_login or not repo_name:
                continue

            # Issues
            try:
                issues = await self._fetch_all_issues(owner_login, repo_name)
                found += len(issues)
                for issue in issues:
                    # GitHub /issues endpoint returns both issues and PRs;
                    # skip PRs here — they are fetched separately below.
                    if issue.get("pull_request"):
                        found -= 1
                        continue
                    try:
                        doc = normalize_issue(issue, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except GitHubError:
                # Skip repos we can't access (e.g. archived, no-issue repos)
                pass

            # Pull Requests
            try:
                prs = await self._fetch_all_pull_requests(owner_login, repo_name)
                found += len(prs)
                for pr in prs:
                    try:
                        doc = normalize_pr(pr, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except GitHubError:
                pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_repos(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        if self._org:
            return await with_retry(self.http_client.list_org_repos, self._org, SYNC_PAGE_SIZE)
        return await with_retry(self.http_client.list_user_repos, SYNC_PAGE_SIZE)

    async def _fetch_all_issues(self, owner: str, repo: str) -> list[dict[str, Any]]:
        assert self.http_client is not None
        return await with_retry(
            self.http_client.list_issues, owner, repo, "open", SYNC_PAGE_SIZE
        )

    async def _fetch_all_pull_requests(self, owner: str, repo: str) -> list[dict[str, Any]]:
        assert self.http_client is not None
        return await with_retry(
            self.http_client.list_pull_requests, owner, repo, "open", SYNC_PAGE_SIZE
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Repos ─────────────────────────────────────────────────────────────────

    async def list_repos(self, org: str | None = None) -> list[dict[str, Any]]:
        """List repositories — org-level if org is provided, otherwise user-level."""
        client = self._ensure_client()
        if org:
            return await with_retry(client.list_org_repos, org, SYNC_PAGE_SIZE)
        return await with_retry(client.list_user_repos, SYNC_PAGE_SIZE)

    # ── Issues ────────────────────────────────────────────────────────────────

    async def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
    ) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/issues."""
        client = self._ensure_client()
        return await with_retry(client.list_issues, owner, repo, state, SYNC_PAGE_SIZE)

    async def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """GET /repos/{owner}/{repo}/issues/{number}."""
        client = self._ensure_client()
        return await with_retry(client.get_issue, owner, repo, number)

    # ── Pull Requests ─────────────────────────────────────────────────────────

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        state: str = "open",
    ) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/pulls."""
        client = self._ensure_client()
        return await with_retry(client.list_pull_requests, owner, repo, state, SYNC_PAGE_SIZE)

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """GET /repos/{owner}/{repo}/pulls/{number}."""
        client = self._ensure_client()
        return await with_retry(client.get_pull_request, owner, repo, number)

    # ── Commits ───────────────────────────────────────────────────────────────

    async def list_commits(self, owner: str, repo: str) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/commits."""
        client = self._ensure_client()
        return await with_retry(client.list_commits, owner, repo, SYNC_PAGE_SIZE)

    # ── Members ───────────────────────────────────────────────────────────────

    async def list_members(self, org: str | None = None) -> list[dict[str, Any]]:
        """GET /orgs/{org}/members — org defaults to self._org when not provided."""
        effective_org = org or self._org
        if not effective_org:
            return []
        client = self._ensure_client()
        return await with_retry(client.list_members, effective_org, SYNC_PAGE_SIZE)

    # ── Single repo ───────────────────────────────────────────────────────────

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        """GET /repos/{owner}/{repo}."""
        client = self._ensure_client()
        return await with_retry(client.get_repo, owner, repo)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> GitHubHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> GitHubConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
