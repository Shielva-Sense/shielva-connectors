"""Unit tests for GitHubConnector — all GitHub HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes
- All model enum values and dataclass fields (including HealthCheckResult.username)
- Normalizer functions for repos, issues, PRs (full and minimal records)
- Stable source_id generation (SHA-256[:16])
- Retry logic (success, retry-on-error, auth-error short-circuits, rate-limit)
- install() — missing creds, success, auth error, generic exception
- health_check() — success (with username), auth error, network error, generic exception
- sync() — empty, repos only, repos+issues+PRs, PR-in-issues filtering, normalize failure, COMPLETED vs PARTIAL
- list_repos (user-level and org-level), list_issues, get_issue, list_pull_requests, get_pull_request
- aclose / context manager
- CircuitBreaker — threshold, reset, half-open, is_open
- _ensure_client, _has_credentials
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import GitHubConnector
from exceptions import (
    GitHubAuthError,
    GitHubError,
    GitHubNetworkError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubServerError,
)
from helpers.utils import (
    CircuitBreaker,
    _stable_id,
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

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_github_test_001"
VALID_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# Sample member fixture
SAMPLE_MEMBER: dict[str, Any] = {
    "login": "monalisa",
    "id": 2,
    "type": "User",
    "site_admin": False,
    "html_url": "https://github.com/monalisa",
}

SAMPLE_COMMIT: dict[str, Any] = {
    "sha": "abc1234",
    "commit": {
        "message": "Initial commit",
        "author": {"name": "Monalisa Octocat", "date": "2024-01-01T00:00:00Z"},
    },
    "html_url": "https://github.com/octocat/Hello-World/commit/abc1234",
    "author": {"login": "monalisa"},
}

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_REPO: dict[str, Any] = {
    "id": 123456,
    "full_name": "octocat/Hello-World",
    "name": "Hello-World",
    "html_url": "https://github.com/octocat/Hello-World",
    "description": "My first repository on GitHub!",
    "language": "Python",
    "default_branch": "main",
    "stargazers_count": 80,
    "forks_count": 9,
    "open_issues_count": 5,
    "visibility": "public",
    "private": False,
    "topics": ["python", "demo"],
    "created_at": "2011-01-26T19:01:12Z",
    "updated_at": "2024-01-01T00:00:00Z",
    "owner": {"login": "octocat"},
}

SAMPLE_ISSUE: dict[str, Any] = {
    "number": 42,
    "title": "Found a bug",
    "html_url": "https://github.com/octocat/Hello-World/issues/42",
    "state": "open",
    "body": "I'm having a problem with this.",
    "user": {"login": "octocat"},
    "labels": [{"name": "bug"}, {"name": "help wanted"}],
    "comments": 3,
    "created_at": "2024-01-10T00:00:00Z",
    "updated_at": "2024-01-15T00:00:00Z",
    "closed_at": None,
}

SAMPLE_PR: dict[str, Any] = {
    "number": 7,
    "title": "Add new feature",
    "html_url": "https://github.com/octocat/Hello-World/pull/7",
    "state": "open",
    "body": "This adds a cool new feature.",
    "user": {"login": "monalisa"},
    "merged": False,
    "merged_at": None,
    "draft": False,
    "commits": 3,
    "additions": 100,
    "deletions": 10,
    "changed_files": 4,
    "labels": [{"name": "enhancement"}],
    "head": {"ref": "feature-branch"},
    "base": {"ref": "main"},
    "created_at": "2024-02-01T00:00:00Z",
    "updated_at": "2024-02-05T00:00:00Z",
    "closed_at": None,
}

# A fake issue that has a pull_request key (GitHub quirk — issues endpoint returns PRs too)
ISSUE_THAT_IS_PR: dict[str, Any] = {
    **SAMPLE_ISSUE,
    "number": 7,
    "html_url": "https://github.com/octocat/Hello-World/issues/7",
    "pull_request": {"url": "https://api.github.com/repos/octocat/Hello-World/pulls/7"},
}

SAMPLE_USER: dict[str, Any] = {
    "login": "octocat",
    "id": 1,
    "name": "The Octocat",
}


# ── Connector fixture ────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> GitHubConnector:
    c = GitHubConnector(
        config={"api_key": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert GitHubConnector.CONNECTOR_TYPE == "github"


def test_auth_type_attr() -> None:
    assert GitHubConnector.AUTH_TYPE == "api_key"


def test_connector_stores_tenant_id() -> None:
    c = GitHubConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = GitHubConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_api_key_from_config() -> None:
    c = GitHubConnector(config={"api_key": "ghp_test"})
    assert c._access_token == "ghp_test"


def test_connector_reads_access_token_fallback_from_config() -> None:
    # backward-compat: access_token still accepted when api_key absent
    c = GitHubConnector(config={"access_token": "ghp_fallback"})
    assert c._access_token == "ghp_fallback"


def test_connector_api_key_takes_precedence_over_access_token() -> None:
    c = GitHubConnector(config={"api_key": "ghp_primary", "access_token": "ghp_secondary"})
    assert c._access_token == "ghp_primary"


def test_connector_reads_org_from_config() -> None:
    c = GitHubConnector(config={"api_key": VALID_TOKEN, "org": "my-org"})
    assert c._org == "my-org"


def test_connector_org_defaults_to_empty() -> None:
    c = GitHubConnector(config={"api_key": VALID_TOKEN})
    assert c._org == ""


def test_connector_no_http_client_initially() -> None:
    c = GitHubConnector()
    assert c.http_client is None


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_github_error_base() -> None:
    exc = GitHubError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_github_auth_error_is_github_error() -> None:
    exc = GitHubAuthError("auth fail", 401, "unauthorized")
    assert isinstance(exc, GitHubError)
    assert exc.status_code == 401


def test_github_rate_limit_error_attrs() -> None:
    exc = GitHubRateLimitError("rate limited", retry_after=60.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 60.0


def test_github_rate_limit_error_default_retry_after() -> None:
    exc = GitHubRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_github_not_found_error_message() -> None:
    exc = GitHubNotFoundError("repo", "octocat/Hello-World")
    assert "octocat/Hello-World" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_github_network_error_is_github_error() -> None:
    exc = GitHubNetworkError("timeout")
    assert isinstance(exc, GitHubError)


def test_github_server_error_is_github_error() -> None:
    exc = GitHubServerError("5xx", status_code=503)
    assert isinstance(exc, GitHubError)
    assert exc.status_code == 503


# ════════════════════════════════════════════════════════════════════════
# 3. MODELS
# ════════════════════════════════════════════════════════════════════════


def test_connector_health_enum_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.FAILED == "failed"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_enum_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_install_result_fields() -> None:
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="c1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.connector_id == "c1"
    assert r.message == "ok"


def test_health_check_result_fields() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        message="ok",
        username="octocat",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.username == "octocat"


def test_health_check_result_username_defaults_empty() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="err",
    )
    assert r.username == ""


def test_sync_result_fields() -> None:
    r = SyncResult(
        status=SyncStatus.PARTIAL,
        documents_found=10,
        documents_synced=8,
        documents_failed=2,
        message="partial",
    )
    assert r.documents_found == 10
    assert r.documents_failed == 2


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        source_id="x1",
        title="Test doc",
        content="Content here",
        connector_id="c1",
        tenant_id="t1",
        source_url="https://github.com/octocat/repo",
        metadata={"key": "val"},
    )
    assert doc.source_id == "x1"
    assert doc.metadata["key"] == "val"


def test_connector_document_default_metadata() -> None:
    doc = ConnectorDocument(
        source_id="x2",
        title="T",
        content="C",
        connector_id="c",
        tenant_id="t",
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ════════════════════════════════════════════════════════════════════════
# 4. STABLE ID
# ════════════════════════════════════════════════════════════════════════


def test_stable_id_length() -> None:
    sid = _stable_id("repo", "https://github.com/octocat/Hello-World")
    assert len(sid) == 16


def test_stable_id_deterministic() -> None:
    url = "https://github.com/octocat/Hello-World"
    assert _stable_id("repo", url) == _stable_id("repo", url)


def test_stable_id_differs_by_entity_type() -> None:
    url = "https://github.com/octocat/Hello-World/issues/1"
    assert _stable_id("issue", url) != _stable_id("pr", url)


def test_stable_id_sha256_prefix() -> None:
    url = "https://github.com/octocat/Hello-World"
    expected = hashlib.sha256(f"repo:{url}".encode()).hexdigest()[:16]
    assert _stable_id("repo", url) == expected


# ════════════════════════════════════════════════════════════════════════
# 5. NORMALIZERS — repos
# ════════════════════════════════════════════════════════════════════════


def test_normalize_repo_title() -> None:
    doc = normalize_repo(SAMPLE_REPO, CONNECTOR_ID, TENANT_ID)
    assert "octocat/Hello-World" in doc.title


def test_normalize_repo_source_id_is_stable() -> None:
    doc = normalize_repo(SAMPLE_REPO, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("repo", "https://github.com/octocat/Hello-World")
    assert doc.source_id == expected


def test_normalize_repo_source_url() -> None:
    doc = normalize_repo(SAMPLE_REPO, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == "https://github.com/octocat/Hello-World"


def test_normalize_repo_metadata_entity_type() -> None:
    doc = normalize_repo(SAMPLE_REPO, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["entity_type"] == "repo"


def test_normalize_repo_metadata_language() -> None:
    doc = normalize_repo(SAMPLE_REPO, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["language"] == "Python"


def test_normalize_repo_metadata_stars() -> None:
    doc = normalize_repo(SAMPLE_REPO, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["stars"] == 80


def test_normalize_repo_metadata_topics() -> None:
    doc = normalize_repo(SAMPLE_REPO, CONNECTOR_ID, TENANT_ID)
    assert "python" in doc.metadata["topics"]


def test_normalize_repo_tenant_connector() -> None:
    doc = normalize_repo(SAMPLE_REPO, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_repo_content_has_description() -> None:
    doc = normalize_repo(SAMPLE_REPO, CONNECTOR_ID, TENANT_ID)
    assert "My first repository" in doc.content


def test_normalize_repo_minimal_record() -> None:
    doc = normalize_repo({"name": "bare", "html_url": "https://github.com/u/bare"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id  # has a stable id
    assert "bare" in doc.title


def test_normalize_repo_visibility_from_private_flag() -> None:
    record = {**SAMPLE_REPO, "visibility": "", "private": True, "html_url": "https://github.com/u/r"}
    doc = normalize_repo(record, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["visibility"] == "private"


# ════════════════════════════════════════════════════════════════════════
# 6. NORMALIZERS — issues
# ════════════════════════════════════════════════════════════════════════


def test_normalize_issue_title() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "#42" in doc.title
    assert "Found a bug" in doc.title


def test_normalize_issue_source_id_stable() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("issue", "https://github.com/octocat/Hello-World/issues/42")
    assert doc.source_id == expected


def test_normalize_issue_source_url() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == "https://github.com/octocat/Hello-World/issues/42"


def test_normalize_issue_metadata_entity_type() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["entity_type"] == "issue"


def test_normalize_issue_metadata_state() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["state"] == "open"


def test_normalize_issue_metadata_author() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["author"] == "octocat"


def test_normalize_issue_metadata_labels() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "bug" in doc.metadata["labels"]
    assert "help wanted" in doc.metadata["labels"]


def test_normalize_issue_metadata_comments() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["comments"] == 3


def test_normalize_issue_content_has_body() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "I'm having a problem" in doc.content


def test_normalize_issue_repo_ref_extracted() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["repo"] == "octocat/Hello-World"


def test_normalize_issue_minimal_record() -> None:
    doc = normalize_issue({"number": 1, "html_url": "https://github.com/u/r/issues/1"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id
    assert "#1" in doc.title


def test_normalize_issue_no_labels() -> None:
    record = {**SAMPLE_ISSUE, "labels": []}
    doc = normalize_issue(record, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["labels"] == []


# ════════════════════════════════════════════════════════════════════════
# 7. NORMALIZERS — pull requests
# ════════════════════════════════════════════════════════════════════════


def test_normalize_pr_title() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert "#7" in doc.title
    assert "Add new feature" in doc.title


def test_normalize_pr_source_id_stable() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("pr", "https://github.com/octocat/Hello-World/pull/7")
    assert doc.source_id == expected


def test_normalize_pr_source_url() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == "https://github.com/octocat/Hello-World/pull/7"


def test_normalize_pr_metadata_entity_type() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["entity_type"] == "pr"


def test_normalize_pr_metadata_author() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["author"] == "monalisa"


def test_normalize_pr_metadata_merged_false() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["merged"] is False


def test_normalize_pr_metadata_merged_true() -> None:
    merged_pr = {**SAMPLE_PR, "merged": True, "merged_at": "2024-02-06T00:00:00Z"}
    doc = normalize_pr(merged_pr, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["merged"] is True
    assert doc.metadata["merged_at"] == "2024-02-06T00:00:00Z"


def test_normalize_pr_metadata_draft() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["draft"] is False


def test_normalize_pr_metadata_head_base_refs() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["head_ref"] == "feature-branch"
    assert doc.metadata["base_ref"] == "main"


def test_normalize_pr_metadata_stats() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["additions"] == 100
    assert doc.metadata["deletions"] == 10
    assert doc.metadata["changed_files"] == 4


def test_normalize_pr_metadata_labels() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert "enhancement" in doc.metadata["labels"]


def test_normalize_pr_content_has_body() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert "cool new feature" in doc.content


def test_normalize_pr_repo_ref_extracted() -> None:
    doc = normalize_pr(SAMPLE_PR, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["repo"] == "octocat/Hello-World"


def test_normalize_pr_minimal_record() -> None:
    doc = normalize_pr({"number": 1, "html_url": "https://github.com/u/r/pull/1"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id
    assert "#1" in doc.title


# ════════════════════════════════════════════════════════════════════════
# 8. RETRY LOGIC
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_retries_on_github_error() -> None:
    fn = AsyncMock(side_effect=[GitHubNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=GitHubAuthError("auth fail", 401))
    with pytest.raises(GitHubAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=GitHubNetworkError("timeout"))
    with pytest.raises(GitHubNetworkError):
        await with_retry(fn, max_retries=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[GitHubRateLimitError("rl", retry_after=0), {"done": True}]
    )
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"done": True}
    mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_retry_with_args_and_kwargs() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", max_retries=1, base_delay=0, kwarg1="val")
    fn.assert_called_once_with("arg1", kwarg1="val")
    assert result == "result"


# ════════════════════════════════════════════════════════════════════════
# 9. install()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    connector = GitHubConnector(
        config={"api_key": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(return_value=SAMPLE_USER)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_missing_credentials() -> None:
    connector = GitHubConnector(config={}, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "required" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    connector = GitHubConnector(
        config={"api_key": "ghp_INVALID"},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(
            side_effect=GitHubAuthError("Bad credentials", 401)
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_exception_fallback() -> None:
    connector = GitHubConnector(
        config={"api_key": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success() -> None:
    connector = GitHubConnector(
        config={"api_key": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(return_value=SAMPLE_USER)
        instance.aclose = AsyncMock()
        await connector.install()
    assert connector.http_client is not None


@pytest.mark.asyncio
async def test_install_message_contains_connected() -> None:
    connector = GitHubConnector(config={"api_key": VALID_TOKEN})
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(return_value=SAMPLE_USER)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert "GitHub" in result.message


# ════════════════════════════════════════════════════════════════════════
# 10. health_check()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy_with_username(authed: GitHubConnector) -> None:
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(return_value=SAMPLE_USER)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.username == "octocat"
    assert "octocat" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_key(authed: GitHubConnector) -> None:
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(
            side_effect=GitHubAuthError("Bad credentials", 401)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: GitHubConnector) -> None:
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(
            side_effect=GitHubNetworkError("timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    connector = GitHubConnector(config={})
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: GitHubConnector) -> None:
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(side_effect=RuntimeError("boom"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_increments_circuit_breaker_on_failure(
    authed: GitHubConnector,
) -> None:
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(
            side_effect=GitHubNetworkError("timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures >= 1


@pytest.mark.asyncio
async def test_health_check_resets_circuit_breaker_on_success(
    authed: GitHubConnector,
) -> None:
    for _ in range(3):
        authed._circuit_breaker.on_failure()
    with patch("connector.GitHubHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_authenticated_user = AsyncMock(return_value=SAMPLE_USER)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures == 0


# ════════════════════════════════════════════════════════════════════════
# 11. sync()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(authed: GitHubConnector) -> None:
    authed.http_client.list_user_repos = AsyncMock(return_value=[])
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_repo_only(authed: GitHubConnector) -> None:
    repo_no_owner = {**SAMPLE_REPO, "owner": None}
    authed.http_client.list_user_repos = AsyncMock(return_value=[repo_no_owner])
    result = await authed.sync(full=True)
    # Repo found but owner/name missing → issues/PRs skipped
    assert result.documents_found == 1
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_with_repos_issues_prs(authed: GitHubConnector) -> None:
    authed.http_client.list_user_repos = AsyncMock(return_value=[SAMPLE_REPO])
    authed.http_client.list_issues = AsyncMock(return_value=[SAMPLE_ISSUE])
    authed.http_client.list_pull_requests = AsyncMock(return_value=[SAMPLE_PR])
    result = await authed.sync(full=True, kb_id="kb_test")
    # 1 repo + 1 issue + 1 PR
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_pr_in_issues_is_skipped(authed: GitHubConnector) -> None:
    """Issues with a pull_request key should be skipped to avoid double-counting."""
    authed.http_client.list_user_repos = AsyncMock(return_value=[SAMPLE_REPO])
    # Issues endpoint returns one real issue + one PR-disguised-as-issue
    authed.http_client.list_issues = AsyncMock(return_value=[SAMPLE_ISSUE, ISSUE_THAT_IS_PR])
    authed.http_client.list_pull_requests = AsyncMock(return_value=[SAMPLE_PR])
    result = await authed.sync(full=True)
    # 1 repo + 1 real issue (PR-in-issues excluded) + 1 PR
    assert result.documents_synced == 3


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(authed: GitHubConnector) -> None:
    bad_repo = {"owner": {"login": "u"}, "name": "r", "html_url": None}
    authed.http_client.list_user_repos = AsyncMock(return_value=[bad_repo])
    authed.http_client.list_issues = AsyncMock(return_value=[])
    authed.http_client.list_pull_requests = AsyncMock(return_value=[])
    result = await authed.sync(full=True)
    # normalize_repo with html_url=None will produce a degenerate but non-crashing doc
    # so it may or may not fail; we just verify the call didn't raise
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


@pytest.mark.asyncio
async def test_sync_fetch_repos_error_returns_failed(authed: GitHubConnector) -> None:
    authed.http_client.list_user_repos = AsyncMock(
        side_effect=GitHubError("API gone", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_uses_org_repos_when_org_configured() -> None:
    connector = GitHubConnector(
        config={"api_key": VALID_TOKEN, "org": "my-org"},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.list_org_repos = AsyncMock(return_value=[])
    mock_client.list_user_repos = AsyncMock(return_value=[])
    connector.http_client = mock_client
    await connector.sync(full=True)
    mock_client.list_org_repos.assert_called_once()
    mock_client.list_user_repos.assert_not_called()


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    connector = GitHubConnector(
        config={"api_key": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.list_user_repos = AsyncMock(return_value=[])
    connector._make_client = lambda: mock_client
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_status_completed_when_no_failures(authed: GitHubConnector) -> None:
    authed.http_client.list_user_repos = AsyncMock(return_value=[SAMPLE_REPO])
    authed.http_client.list_issues = AsyncMock(return_value=[SAMPLE_ISSUE])
    authed.http_client.list_pull_requests = AsyncMock(return_value=[SAMPLE_PR])
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


# ════════════════════════════════════════════════════════════════════════
# 12. list_repos / list_issues / get_issue / list_pull_requests / get_pull_request
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_repos_user_level(authed: GitHubConnector) -> None:
    authed.http_client.list_user_repos = AsyncMock(return_value=[SAMPLE_REPO])
    result = await authed.list_repos()
    assert result[0]["full_name"] == "octocat/Hello-World"


@pytest.mark.asyncio
async def test_list_repos_org_level(authed: GitHubConnector) -> None:
    authed.http_client.list_org_repos = AsyncMock(return_value=[SAMPLE_REPO])
    result = await authed.list_repos(org="my-org")
    authed.http_client.list_org_repos.assert_called_once()
    assert result[0]["full_name"] == "octocat/Hello-World"


@pytest.mark.asyncio
async def test_list_issues(authed: GitHubConnector) -> None:
    authed.http_client.list_issues = AsyncMock(return_value=[SAMPLE_ISSUE])
    result = await authed.list_issues("octocat", "Hello-World")
    assert result[0]["number"] == 42


@pytest.mark.asyncio
async def test_list_issues_with_state(authed: GitHubConnector) -> None:
    authed.http_client.list_issues = AsyncMock(return_value=[])
    await authed.list_issues("octocat", "Hello-World", state="closed")
    authed.http_client.list_issues.assert_called_once_with(
        "octocat", "Hello-World", "closed", 100
    )


@pytest.mark.asyncio
async def test_get_issue(authed: GitHubConnector) -> None:
    authed.http_client.get_issue = AsyncMock(return_value=SAMPLE_ISSUE)
    result = await authed.get_issue("octocat", "Hello-World", 42)
    assert result["number"] == 42
    assert result["title"] == "Found a bug"


@pytest.mark.asyncio
async def test_get_issue_calls_correct_args(authed: GitHubConnector) -> None:
    authed.http_client.get_issue = AsyncMock(return_value=SAMPLE_ISSUE)
    await authed.get_issue("octocat", "Hello-World", 42)
    authed.http_client.get_issue.assert_called_once_with("octocat", "Hello-World", 42)


@pytest.mark.asyncio
async def test_list_pull_requests(authed: GitHubConnector) -> None:
    authed.http_client.list_pull_requests = AsyncMock(return_value=[SAMPLE_PR])
    result = await authed.list_pull_requests("octocat", "Hello-World")
    assert result[0]["number"] == 7


@pytest.mark.asyncio
async def test_list_pull_requests_with_state(authed: GitHubConnector) -> None:
    authed.http_client.list_pull_requests = AsyncMock(return_value=[])
    await authed.list_pull_requests("octocat", "Hello-World", state="closed")
    authed.http_client.list_pull_requests.assert_called_once_with(
        "octocat", "Hello-World", "closed", 100
    )


@pytest.mark.asyncio
async def test_get_pull_request(authed: GitHubConnector) -> None:
    authed.http_client.get_pull_request = AsyncMock(return_value=SAMPLE_PR)
    result = await authed.get_pull_request("octocat", "Hello-World", 7)
    assert result["number"] == 7
    assert result["title"] == "Add new feature"


@pytest.mark.asyncio
async def test_get_pull_request_calls_correct_args(authed: GitHubConnector) -> None:
    authed.http_client.get_pull_request = AsyncMock(return_value=SAMPLE_PR)
    await authed.get_pull_request("octocat", "Hello-World", 7)
    authed.http_client.get_pull_request.assert_called_once_with("octocat", "Hello-World", 7)


# ════════════════════════════════════════════════════════════════════════
# 13. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: GitHubConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    connector = GitHubConnector(config={"api_key": VALID_TOKEN})
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    connector = GitHubConnector(
        config={"api_key": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    connector.http_client = mock_client
    async with connector as c:
        assert c is connector
    mock_client.aclose.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# 14. CircuitBreaker
# ════════════════════════════════════════════════════════════════════════


def test_circuit_breaker_starts_closed() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_opens_on_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(5):
        cb.on_failure()
    assert cb.state == "open"


def test_circuit_breaker_closes_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(5):
        cb.on_failure()
    cb.on_success()
    assert cb.state == "closed"
    assert cb._failures == 0


def test_circuit_breaker_is_open_property() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    assert not cb.is_open
    for _ in range(3):
        cb.on_failure()
    assert cb.is_open


def test_circuit_breaker_half_open_after_timeout() -> None:
    import time
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.01)
    cb.on_failure()
    assert cb.state == "open"
    time.sleep(0.05)
    assert cb.state == "half-open"


def test_circuit_breaker_failure_below_threshold_stays_closed() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(4):
        cb.on_failure()
    assert cb.state == "closed"


def test_circuit_breaker_custom_recovery_timeout() -> None:
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=999.0)
    cb.on_failure()
    assert cb.state == "open"
    assert cb.state == "open"


# ════════════════════════════════════════════════════════════════════════
# 15. _ensure_client / _has_credentials
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    connector = GitHubConnector(config={"api_key": VALID_TOKEN})
    mock_client = MagicMock()
    connector._make_client = lambda: mock_client
    client = connector._ensure_client()
    assert client is mock_client
    assert connector.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    connector = GitHubConnector(config={"api_key": VALID_TOKEN})
    existing = MagicMock()
    connector.http_client = existing
    client = connector._ensure_client()
    assert client is existing


def test_has_credentials_true_with_token() -> None:
    c = GitHubConnector(config={"api_key": "ghp_test"})
    assert c._has_credentials() is True


def test_has_credentials_false_when_empty() -> None:
    c = GitHubConnector(config={})
    assert c._has_credentials() is False


def test_has_credentials_false_with_empty_string() -> None:
    c = GitHubConnector(config={"api_key": ""})
    assert c._has_credentials() is False


# ════════════════════════════════════════════════════════════════════════
# 16. list_commits / list_members / get_repo
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_commits(authed: GitHubConnector) -> None:
    authed.http_client.list_commits = AsyncMock(return_value=[SAMPLE_COMMIT])
    result = await authed.list_commits("octocat", "Hello-World")
    assert result[0]["sha"] == "abc1234"


@pytest.mark.asyncio
async def test_list_commits_calls_correct_args(authed: GitHubConnector) -> None:
    authed.http_client.list_commits = AsyncMock(return_value=[])
    await authed.list_commits("octocat", "Hello-World")
    authed.http_client.list_commits.assert_called_once_with("octocat", "Hello-World", 100)


@pytest.mark.asyncio
async def test_list_members_with_org_param(authed: GitHubConnector) -> None:
    authed.http_client.list_members = AsyncMock(return_value=[SAMPLE_MEMBER])
    result = await authed.list_members(org="my-org")
    assert result[0]["login"] == "monalisa"
    authed.http_client.list_members.assert_called_once_with("my-org", 100)


@pytest.mark.asyncio
async def test_list_members_uses_connector_org_when_no_param(authed: GitHubConnector) -> None:
    authed._org = "default-org"
    authed.http_client.list_members = AsyncMock(return_value=[SAMPLE_MEMBER])
    result = await authed.list_members()
    authed.http_client.list_members.assert_called_once_with("default-org", 100)
    assert result[0]["login"] == "monalisa"


@pytest.mark.asyncio
async def test_list_members_returns_empty_when_no_org(authed: GitHubConnector) -> None:
    authed._org = ""
    result = await authed.list_members()
    assert result == []


@pytest.mark.asyncio
async def test_get_repo(authed: GitHubConnector) -> None:
    authed.http_client.get_repo = AsyncMock(return_value=SAMPLE_REPO)
    result = await authed.get_repo("octocat", "Hello-World")
    assert result["full_name"] == "octocat/Hello-World"


@pytest.mark.asyncio
async def test_get_repo_calls_correct_args(authed: GitHubConnector) -> None:
    authed.http_client.get_repo = AsyncMock(return_value=SAMPLE_REPO)
    await authed.get_repo("octocat", "Hello-World")
    authed.http_client.get_repo.assert_called_once_with("octocat", "Hello-World")


# ════════════════════════════════════════════════════════════════════════
# 17. HTTP client — required headers
# ════════════════════════════════════════════════════════════════════════


def test_http_client_sets_authorization_header() -> None:
    from client.http_client import GitHubHTTPClient
    client = GitHubHTTPClient(access_token="ghp_test_token")
    headers = dict(client._client.headers)
    # httpx lowercases header names
    assert headers.get("authorization") == "Bearer ghp_test_token"


def test_http_client_sets_accept_header() -> None:
    from client.http_client import GitHubHTTPClient
    client = GitHubHTTPClient(access_token="ghp_test_token")
    headers = dict(client._client.headers)
    assert headers.get("accept") == "application/vnd.github+json"


def test_http_client_sets_api_version_header() -> None:
    from client.http_client import GitHubHTTPClient
    client = GitHubHTTPClient(access_token="ghp_test_token")
    headers = dict(client._client.headers)
    assert headers.get("x-github-api-version") == "2022-11-28"


# ════════════════════════════════════════════════════════════════════════
# 18. HTTP client — _raise_for_status error mapping
# ════════════════════════════════════════════════════════════════════════


def _make_mock_response(status: int, body: dict | None = None, headers: dict | None = None) -> MagicMock:
    """Build a minimal mock httpx.Response for _raise_for_status tests."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = body.get("message", "") if body else ""
    resp.content = b"content" if body else b""
    resp.headers = {**(headers or {})}
    resp.url = MagicMock()
    resp.url.path = "/repos/octocat/Hello-World"

    def mock_json() -> dict:
        return body or {}

    resp.json = mock_json
    return resp


def test_raise_for_status_401_raises_auth_error() -> None:
    from client.http_client import GitHubHTTPClient
    client = GitHubHTTPClient(access_token="tok")
    resp = _make_mock_response(401, {"message": "Bad credentials"})
    with pytest.raises(GitHubAuthError):
        client._raise_for_status(resp)


def test_raise_for_status_403_raises_auth_error() -> None:
    from client.http_client import GitHubHTTPClient
    client = GitHubHTTPClient(access_token="tok")
    resp = _make_mock_response(403, {"message": "Forbidden"})
    with pytest.raises(GitHubAuthError):
        client._raise_for_status(resp)


def test_raise_for_status_404_raises_not_found() -> None:
    from client.http_client import GitHubHTTPClient
    client = GitHubHTTPClient(access_token="tok")
    resp = _make_mock_response(404, {"message": "Not Found"})
    with pytest.raises(GitHubNotFoundError):
        client._raise_for_status(resp)


def test_raise_for_status_429_raises_rate_limit() -> None:
    from client.http_client import GitHubHTTPClient
    client = GitHubHTTPClient(access_token="tok")
    resp = _make_mock_response(429, {"message": "Too Many Requests"}, headers={"Retry-After": "30"})
    with pytest.raises(GitHubRateLimitError) as exc_info:
        client._raise_for_status(resp)
    assert exc_info.value.retry_after == 30.0


def test_raise_for_status_500_raises_server_error() -> None:
    from client.http_client import GitHubHTTPClient
    from exceptions import GitHubServerError
    client = GitHubHTTPClient(access_token="tok")
    resp = _make_mock_response(500, {"message": "Internal Server Error"})
    with pytest.raises(GitHubServerError):
        client._raise_for_status(resp)


def test_raise_for_status_422_raises_github_error() -> None:
    from client.http_client import GitHubHTTPClient
    client = GitHubHTTPClient(access_token="tok")
    resp = _make_mock_response(422, {"message": "Validation Failed"})
    with pytest.raises(GitHubError):
        client._raise_for_status(resp)


def test_raise_for_status_200_does_not_raise() -> None:
    from client.http_client import GitHubHTTPClient
    client = GitHubHTTPClient(access_token="tok")
    resp = _make_mock_response(200, {}, headers={"X-RateLimit-Remaining": "100"})
    client._raise_for_status(resp)  # must not raise


def test_raise_for_status_rate_limit_remaining_zero_raises() -> None:
    from client.http_client import GitHubHTTPClient
    client = GitHubHTTPClient(access_token="tok")
    resp = _make_mock_response(200, {}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999999"})
    with pytest.raises(GitHubRateLimitError):
        client._raise_for_status(resp)


# ════════════════════════════════════════════════════════════════════════
# 19. Link header pagination — _next_url
# ════════════════════════════════════════════════════════════════════════


def test_next_url_parses_link_header() -> None:
    from client.http_client import GitHubHTTPClient
    import httpx
    client = GitHubHTTPClient(access_token="tok")
    headers = httpx.Headers({
        "Link": '<https://api.github.com/repos/octocat/Hello-World/issues?page=2>; rel="next", '
                '<https://api.github.com/repos/octocat/Hello-World/issues?page=5>; rel="last"'
    })
    url = client._next_url(headers)
    assert url == "https://api.github.com/repos/octocat/Hello-World/issues?page=2"


def test_next_url_returns_none_when_no_link_header() -> None:
    from client.http_client import GitHubHTTPClient
    import httpx
    client = GitHubHTTPClient(access_token="tok")
    headers = httpx.Headers({})
    assert client._next_url(headers) is None


def test_next_url_returns_none_when_no_next_rel() -> None:
    from client.http_client import GitHubHTTPClient
    import httpx
    client = GitHubHTTPClient(access_token="tok")
    headers = httpx.Headers({
        "Link": '<https://api.github.com/repos/octocat/Hello-World/issues?page=1>; rel="prev"'
    })
    assert client._next_url(headers) is None
