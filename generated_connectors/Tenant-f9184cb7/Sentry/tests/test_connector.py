"""Unit tests for SentryConnector — all HTTP calls are mocked via AsyncMock.

Coverage targets:
  exceptions      (5+)
  models          (5+)
  normalize_*     (8+)
  with_retry      (6+)
  HTTP client     (14+)
  install         (5+)
  health_check    (5+)
  sync            (8+)
  list_* methods  (5+)
  get_issue/events(4+)
  Total: 65+
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import SentryConnector
from exceptions import (
    SentryAuthError,
    SentryError,
    SentryNetworkError,
    SentryNotFoundError,
    SentryRateLimitError,
)
from helpers.utils import (
    normalize_event,
    normalize_issue,
    normalize_project,
    normalize_release,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ──────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_sentry_test_001"
AUTH_TOKEN = "sntrys_test_token_abc123XYZ"
ORG_SLUG = "my-org"

SAMPLE_ORG: dict = {
    "id": "1",
    "slug": ORG_SLUG,
    "name": "My Organization",
    "status": {"id": "active", "name": "active"},
}

SAMPLE_PROJECT: dict = {
    "id": "proj-001",
    "slug": "my-backend",
    "name": "My Backend",
    "status": "active",
    "platform": "python",
    "dateCreated": "2026-01-01T00:00:00Z",
    "isPublic": False,
    "teams": [{"id": "t1", "name": "Platform Team"}],
}

SAMPLE_ISSUE: dict = {
    "id": "12345",
    "title": "ZeroDivisionError: division by zero",
    "culprit": "myapp/views.py in divide",
    "status": "unresolved",
    "level": "error",
    "firstSeen": "2026-06-01T08:00:00Z",
    "lastSeen": "2026-06-19T12:00:00Z",
    "timesSeen": 42,
    "permalink": "https://sentry.io/organizations/my-org/issues/12345/",
    "project": {"id": "proj-001", "slug": "my-backend", "name": "My Backend"},
    "assignedTo": {"name": "alice@example.com"},
    "metadata": {"type": "ZeroDivisionError", "value": "division by zero"},
}

SAMPLE_RELEASE: dict = {
    "version": "v1.2.3-abc1234",
    "shortVersion": "1.2.3",
    "dateCreated": "2026-06-15T10:00:00Z",
    "dateReleased": "2026-06-15T11:00:00Z",
    "url": "https://github.com/my-org/my-repo/releases/v1.2.3",
    "authors": [{"name": "Alice", "username": "alice"}],
    "projects": [{"slug": "my-backend", "name": "My Backend"}],
    "commitCount": 5,
    "newGroups": 2,
}

SAMPLE_EVENT: dict = {
    "id": "evt-001",
    "eventID": "evt-001abc",
    "title": "ZeroDivisionError: division by zero",
    "platform": "python",
    "dateCreated": "2026-06-19T12:00:00Z",
    "culprit": "myapp/views.py in divide",
    "level": "error",
    "tags": [
        {"key": "environment", "value": "production"},
        {"key": "release", "value": "1.2.3"},
    ],
}


def _make_connector(
    auth_token: str = AUTH_TOKEN,
    org_slug: str = ORG_SLUG,
    base_url: str = "https://sentry.io",
) -> SentryConnector:
    return SentryConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "auth_token": auth_token,
            "organization_slug": org_slug,
            "base_url": base_url,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# Exceptions (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_sentry_error_base(self) -> None:
        exc = SentryError("base error", status_code=400, code="bad_request")
        assert str(exc) == "base error"
        assert exc.status_code == 400
        assert exc.code == "bad_request"

    def test_sentry_auth_error_is_sentry_error(self) -> None:
        exc = SentryAuthError("auth failed", status_code=401, code="auth_error")
        assert isinstance(exc, SentryError)
        assert exc.status_code == 401

    def test_sentry_rate_limit_error_defaults(self) -> None:
        exc = SentryRateLimitError("rate limited")
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 0.0

    def test_sentry_not_found_error_message(self) -> None:
        exc = SentryNotFoundError("issue", "99999")
        assert "99999" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"

    def test_sentry_network_error_is_sentry_error(self) -> None:
        exc = SentryNetworkError("timeout", status_code=504)
        assert isinstance(exc, SentryError)
        assert exc.status_code == 504


# ══════════════════════════════════════════════════════════════════════════════
# Models (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_connector_health_values(self) -> None:
        from models import ConnectorHealth
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        from models import AuthStatus
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        from models import SyncStatus
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_install_result_fields(self) -> None:
        from models import InstallResult
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="c123",
            message="ok",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.connector_id == "c123"

    def test_connector_document_fields(self) -> None:
        from models import ConnectorDocument
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="content",
            connector_id="conn1",
            tenant_id="tenant1",
            source_url="https://example.com",
            metadata={"key": "val"},
        )
        assert doc.source_id == "abc123"
        assert doc.metadata["key"] == "val"


# ══════════════════════════════════════════════════════════════════════════════
# Normalize functions (8 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestNormalizeIssue:
    def test_basic_fields(self) -> None:
        doc = normalize_issue(SAMPLE_ISSUE, ORG_SLUG)
        assert doc.title == SAMPLE_ISSUE["title"]
        assert "ZeroDivisionError" in doc.content
        assert doc.source_url == SAMPLE_ISSUE["permalink"]

    def test_source_id_is_16_chars_hex(self) -> None:
        doc = normalize_issue(SAMPLE_ISSUE, ORG_SLUG)
        assert len(doc.source_id) == 16
        assert all(c in "0123456789abcdef" for c in doc.source_id)

    def test_metadata_contains_issue_fields(self) -> None:
        doc = normalize_issue(SAMPLE_ISSUE, ORG_SLUG)
        assert doc.metadata["issue_id"] == "12345"
        assert doc.metadata["org_slug"] == ORG_SLUG
        assert doc.metadata["status"] == "unresolved"
        assert doc.metadata["level"] == "error"
        assert doc.metadata["times_seen"] == 42

    def test_source_id_stable(self) -> None:
        doc1 = normalize_issue(SAMPLE_ISSUE, ORG_SLUG)
        doc2 = normalize_issue(SAMPLE_ISSUE, ORG_SLUG)
        assert doc1.source_id == doc2.source_id

    def test_empty_issue(self) -> None:
        doc = normalize_issue({})
        assert "Issue" in doc.title
        assert doc.source_id  # must still produce a 16-char hash


class TestNormalizeProject:
    def test_basic_fields(self) -> None:
        doc = normalize_project(SAMPLE_PROJECT, ORG_SLUG)
        assert doc.title == "My Backend"
        assert "python" in doc.content.lower()

    def test_source_id_hash_prefix(self) -> None:
        doc = normalize_project(SAMPLE_PROJECT, ORG_SLUG)
        assert len(doc.source_id) == 16

    def test_metadata_org_slug(self) -> None:
        doc = normalize_project(SAMPLE_PROJECT, ORG_SLUG)
        assert doc.metadata["org_slug"] == ORG_SLUG
        assert doc.metadata["slug"] == "my-backend"


class TestNormalizeRelease:
    def test_basic_fields(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE, ORG_SLUG)
        assert "1.2.3" in doc.title
        assert "Alice" in doc.content

    def test_source_id_includes_org_and_version(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE, ORG_SLUG)
        assert len(doc.source_id) == 16
        # Different org slug → different source_id
        doc2 = normalize_release(SAMPLE_RELEASE, "other-org")
        assert doc.source_id != doc2.source_id

    def test_metadata_commit_count(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE, ORG_SLUG)
        assert doc.metadata["commit_count"] == 5
        assert doc.metadata["new_groups"] == 2


class TestNormalizeEvent:
    def test_basic_fields(self) -> None:
        doc = normalize_event(SAMPLE_EVENT, issue_id="12345")
        assert "ZeroDivisionError" in doc.title
        assert "production" in doc.content

    def test_source_id_from_event_id(self) -> None:
        doc = normalize_event(SAMPLE_EVENT, "12345")
        assert len(doc.source_id) == 16

    def test_metadata_issue_id(self) -> None:
        doc = normalize_event(SAMPLE_EVENT, "12345")
        assert doc.metadata["issue_id"] == "12345"
        assert doc.metadata["level"] == "error"

    def test_empty_event_fallback_title(self) -> None:
        doc = normalize_event({}, "")
        assert "Event" in doc.title


# ══════════════════════════════════════════════════════════════════════════════
# with_retry (6 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[SentryNetworkError("timeout"), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=SentryAuthError("bad token"))
        with pytest.raises(SentryAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    async def test_raises_after_max_attempts(self) -> None:
        err = SentryNetworkError("persistent error")
        fn = AsyncMock(side_effect=err)
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(SentryNetworkError):
                await with_retry(fn, max_attempts=3)
        assert fn.call_count == 3

    async def test_rate_limit_retry(self) -> None:
        fn = AsyncMock(
            side_effect=[
                SentryRateLimitError("rate limited", retry_after=0.0),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}

    async def test_passes_args_and_kwargs(self) -> None:
        fn = AsyncMock(return_value="result")
        result = await with_retry(fn, "arg1", kwarg1="val1", max_attempts=2)
        fn.assert_called_once_with("arg1", kwarg1="val1")
        assert result == "result"


# ══════════════════════════════════════════════════════════════════════════════
# HTTP client (mocked) (14 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestSentryHTTPClient:
    def _make_client(self, base_url: str = "https://sentry.io") -> "SentryHTTPClient":
        from client.http_client import SentryHTTPClient
        return SentryHTTPClient(
            config={"auth_token": AUTH_TOKEN, "base_url": base_url}
        )

    def test_bearer_auth_header(self) -> None:
        client = self._make_client()
        headers = client._make_headers()
        assert headers["Authorization"] == f"Bearer {AUTH_TOKEN}"

    def test_api_base_url_default(self) -> None:
        client = self._make_client()
        assert client._api_base == "https://sentry.io/api/0"

    def test_api_base_url_self_hosted(self) -> None:
        client = self._make_client(base_url="https://sentry.mycompany.com")
        assert client._api_base == "https://sentry.mycompany.com/api/0"

    def test_api_base_url_strips_trailing_slash(self) -> None:
        client = self._make_client(base_url="https://sentry.io/")
        assert client._api_base == "https://sentry.io/api/0"

    async def test_get_organization_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=(SAMPLE_ORG, {}))
        result = await client.get_organization(ORG_SLUG)
        assert result["slug"] == ORG_SLUG
        client._request.assert_called_once_with("GET", f"/organizations/{ORG_SLUG}/")

    async def test_get_projects_single_page(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(
            return_value=([SAMPLE_PROJECT], {"Link": ""})
        )
        result = await client.get_projects(ORG_SLUG)
        assert len(result) == 1
        assert result[0]["slug"] == "my-backend"

    async def test_get_issues_no_cursor(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(
            return_value=([SAMPLE_ISSUE], {"Link": ""})
        )
        items, next_cursor = await client.get_issues(ORG_SLUG)
        assert len(items) == 1
        assert next_cursor is None

    async def test_get_issues_with_cursor_pagination(self) -> None:
        client = self._make_client()
        link_next = (
            '<https://sentry.io/api/0/organizations/my-org/issues/?cursor=abc>; '
            'rel="next"; results="true"'
        )
        client._request = AsyncMock(
            return_value=([SAMPLE_ISSUE], {"Link": link_next})
        )
        items, next_cursor = await client.get_issues(ORG_SLUG)
        assert next_cursor is not None
        assert "cursor=abc" in next_cursor

    async def test_get_issue_single(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=(SAMPLE_ISSUE, {}))
        result = await client.get_issue("12345")
        assert result["id"] == "12345"
        client._request.assert_called_once_with("GET", "/issues/12345/")

    async def test_get_issue_events(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=([SAMPLE_EVENT], {}))
        result = await client.get_issue_events("12345")
        assert len(result) == 1
        assert result[0]["id"] == "evt-001"

    async def test_get_releases_no_cursor(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(
            return_value=([SAMPLE_RELEASE], {"Link": ""})
        )
        items, next_cursor = await client.get_releases(ORG_SLUG)
        assert len(items) == 1
        assert next_cursor is None

    async def test_raise_for_status_401(self) -> None:
        client = self._make_client()
        with pytest.raises(SentryAuthError) as exc_info:
            client._raise_for_status(401, {"detail": "Unauthorized"})
        assert exc_info.value.status_code == 401

    async def test_raise_for_status_403(self) -> None:
        client = self._make_client()
        with pytest.raises(SentryAuthError) as exc_info:
            client._raise_for_status(403, {"detail": "Forbidden"})
        assert exc_info.value.status_code == 403

    async def test_raise_for_status_404(self) -> None:
        client = self._make_client()
        with pytest.raises(SentryNotFoundError):
            client._raise_for_status(404, {})

    async def test_raise_for_status_429(self) -> None:
        client = self._make_client()
        with pytest.raises(SentryRateLimitError):
            client._raise_for_status(429, {})

    async def test_raise_for_status_500(self) -> None:
        client = self._make_client()
        with pytest.raises(SentryNetworkError) as exc_info:
            client._raise_for_status(500, {"detail": "Internal Server Error"})
        assert exc_info.value.status_code == 500

    async def test_link_header_no_next_when_results_false(self) -> None:
        from client.http_client import _parse_next_cursor
        link = '<https://sentry.io/api/0/issues/?cursor=abc>; rel="next"; results="false"'
        assert _parse_next_cursor(link) is None

    async def test_link_header_next_cursor_parsed(self) -> None:
        from client.http_client import _parse_next_cursor
        link = (
            '<https://sentry.io/api/0/issues/?cursor=100:0:0>; rel="next"; results="true"'
        )
        result = _parse_next_cursor(link)
        assert result is not None
        assert "cursor=100:0:0" in result

    async def test_link_header_empty_string(self) -> None:
        from client.http_client import _parse_next_cursor
        assert _parse_next_cursor("") is None


# ══════════════════════════════════════════════════════════════════════════════
# install (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestInstall:
    async def test_install_success(self) -> None:
        conn = _make_connector()
        conn.client.get_organization = AsyncMock(return_value=SAMPLE_ORG)
        result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "My Organization" in result.message

    async def test_install_missing_auth_token(self) -> None:
        conn = _make_connector(auth_token="")
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "auth_token" in result.message

    async def test_install_missing_org_slug(self) -> None:
        conn = _make_connector(org_slug="")
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "organization_slug" in result.message

    async def test_install_auth_error(self) -> None:
        conn = _make_connector()
        conn.client.get_organization = AsyncMock(
            side_effect=SentryAuthError("Invalid token", status_code=401)
        )
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        conn = _make_connector()
        conn.client.get_organization = AsyncMock(
            side_effect=SentryNetworkError("Connection refused")
        )
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ══════════════════════════════════════════════════════════════════════════════
# health_check (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    async def test_health_check_success(self) -> None:
        conn = _make_connector()
        conn.client.get_organization = AsyncMock(return_value=SAMPLE_ORG)
        result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "My Organization" in result.message

    async def test_health_check_missing_credentials(self) -> None:
        conn = _make_connector(auth_token="", org_slug="")
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        conn = _make_connector()
        conn.client.get_organization = AsyncMock(
            side_effect=SentryAuthError("Forbidden", status_code=403)
        )
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        conn = _make_connector()
        conn.client.get_organization = AsyncMock(
            side_effect=SentryNetworkError("Timeout")
        )
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_generic_error(self) -> None:
        conn = _make_connector()
        conn.client.get_organization = AsyncMock(
            side_effect=Exception("Unknown error")
        )
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ══════════════════════════════════════════════════════════════════════════════
# sync (8 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestSync:
    def _patched_connector(self) -> SentryConnector:
        conn = _make_connector()
        doc_proj = normalize_project(SAMPLE_PROJECT, ORG_SLUG)
        doc_proj.connector_id = CONNECTOR_ID
        doc_proj.tenant_id = TENANT_ID
        doc_issue = normalize_issue(SAMPLE_ISSUE, ORG_SLUG)
        doc_issue.connector_id = CONNECTOR_ID
        doc_issue.tenant_id = TENANT_ID
        doc_release = normalize_release(SAMPLE_RELEASE, ORG_SLUG)
        doc_release.connector_id = CONNECTOR_ID
        doc_release.tenant_id = TENANT_ID

        conn.list_projects = AsyncMock(return_value=[doc_proj])
        conn.list_issues = AsyncMock(return_value=[doc_issue])
        conn.list_releases = AsyncMock(return_value=[doc_release])
        return conn

    async def test_sync_completed_status(self) -> None:
        conn = self._patched_connector()
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_counts_all_resources(self) -> None:
        conn = self._patched_connector()
        result = await conn.sync()
        # 1 project + 1 issue (per project) + 1 release = 3
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.documents_failed == 0

    async def test_sync_multiple_projects_issues(self) -> None:
        conn = _make_connector()
        proj1 = normalize_project(SAMPLE_PROJECT, ORG_SLUG)
        proj1.metadata["slug"] = "proj-a"
        proj2 = normalize_project({**SAMPLE_PROJECT, "id": "p2", "slug": "proj-b", "name": "B"}, ORG_SLUG)
        proj2.metadata["slug"] = "proj-b"
        for p in [proj1, proj2]:
            p.connector_id = CONNECTOR_ID
            p.tenant_id = TENANT_ID

        doc_issue = normalize_issue(SAMPLE_ISSUE, ORG_SLUG)
        doc_issue.connector_id = CONNECTOR_ID
        doc_issue.tenant_id = TENANT_ID
        doc_release = normalize_release(SAMPLE_RELEASE, ORG_SLUG)
        doc_release.connector_id = CONNECTOR_ID
        doc_release.tenant_id = TENANT_ID

        conn.list_projects = AsyncMock(return_value=[proj1, proj2])
        conn.list_issues = AsyncMock(return_value=[doc_issue])
        conn.list_releases = AsyncMock(return_value=[doc_release])

        result = await conn.sync()
        assert result.documents_found == 5  # 2 projects + 2 issues + 1 release
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_projects_failure_returns_failed(self) -> None:
        conn = _make_connector()
        conn.list_projects = AsyncMock(
            side_effect=SentryNetworkError("API down")
        )
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_releases_failure_returns_failed(self) -> None:
        conn = _make_connector()
        conn.list_projects = AsyncMock(return_value=[])
        conn.list_issues = AsyncMock(return_value=[])
        conn.list_releases = AsyncMock(
            side_effect=SentryNetworkError("API down")
        )
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_partial_on_ingest_error(self) -> None:
        conn = _make_connector()
        doc_proj = normalize_project(SAMPLE_PROJECT, ORG_SLUG)
        doc_proj.metadata["slug"] = "my-backend"
        doc_proj.connector_id = CONNECTOR_ID
        doc_proj.tenant_id = TENANT_ID

        conn.list_projects = AsyncMock(return_value=[doc_proj])
        conn.list_issues = AsyncMock(return_value=[])
        conn.list_releases = AsyncMock(return_value=[])

        async def _bad_ingest(doc: object, kb_id: str) -> None:
            raise RuntimeError("ingest failed")

        conn._ingest_document = _bad_ingest  # type: ignore[method-assign]
        result = await conn.sync(kb_id="kb123")
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed == 1

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        conn = _make_connector()
        doc_proj = normalize_project(SAMPLE_PROJECT, ORG_SLUG)
        doc_proj.metadata["slug"] = "my-backend"
        doc_proj.connector_id = CONNECTOR_ID
        doc_proj.tenant_id = TENANT_ID

        conn.list_projects = AsyncMock(return_value=[doc_proj])
        conn.list_issues = AsyncMock(return_value=[])
        conn.list_releases = AsyncMock(return_value=[])
        conn._ingest_document = AsyncMock()

        await conn.sync(kb_id="kb123")
        conn._ingest_document.assert_called_once()

    async def test_sync_no_kb_id_skips_ingest(self) -> None:
        conn = self._patched_connector()
        conn._ingest_document = AsyncMock()
        await conn.sync(kb_id="")
        conn._ingest_document.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# list_* methods (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestListMethods:
    async def test_list_projects_returns_docs(self) -> None:
        conn = _make_connector()
        conn.client.get_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
        docs = await conn.list_projects()
        assert len(docs) == 1
        assert docs[0].title == "My Backend"
        assert docs[0].tenant_id == TENANT_ID
        assert docs[0].connector_id == CONNECTOR_ID

    async def test_list_issues_single_page(self) -> None:
        conn = _make_connector()
        conn.client.get_issues = AsyncMock(return_value=([SAMPLE_ISSUE], None))
        docs = await conn.list_issues()
        assert len(docs) == 1
        assert docs[0].metadata["issue_id"] == "12345"

    async def test_list_issues_cursor_pagination(self) -> None:
        conn = _make_connector()
        # First call returns cursor, second returns empty to stop
        conn.client.get_issues = AsyncMock(
            side_effect=[
                ([SAMPLE_ISSUE], "https://sentry.io/api/0/issues/?cursor=next"),
                ([SAMPLE_ISSUE], None),
            ]
        )
        docs = await conn.list_issues()
        assert len(docs) == 2

    async def test_list_releases_single_page(self) -> None:
        conn = _make_connector()
        conn.client.get_releases = AsyncMock(return_value=([SAMPLE_RELEASE], None))
        docs = await conn.list_releases()
        assert len(docs) == 1
        assert "1.2.3" in docs[0].title

    async def test_list_issues_with_project_filter(self) -> None:
        conn = _make_connector()
        conn.client.get_issues = AsyncMock(return_value=([SAMPLE_ISSUE], None))
        docs = await conn.list_issues(project_slug="my-backend")
        conn.client.get_issues.assert_called_once_with(
            ORG_SLUG, project="my-backend", cursor=None, limit=100
        )
        assert len(docs) == 1


# ══════════════════════════════════════════════════════════════════════════════
# get_issue and list_issue_events (4 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestGetIssueAndEvents:
    async def test_get_issue_returns_raw_dict(self) -> None:
        conn = _make_connector()
        conn.client.get_issue = AsyncMock(return_value=SAMPLE_ISSUE)
        result = await conn.get_issue("12345")
        assert result["id"] == "12345"
        assert result["title"] == SAMPLE_ISSUE["title"]

    async def test_get_issue_not_found(self) -> None:
        conn = _make_connector()
        conn.client.get_issue = AsyncMock(
            side_effect=SentryNotFoundError("issue", "99999")
        )
        with pytest.raises(SentryNotFoundError):
            await conn.get_issue("99999")

    async def test_list_issue_events_returns_docs(self) -> None:
        conn = _make_connector()
        conn.client.get_issue_events = AsyncMock(return_value=[SAMPLE_EVENT])
        docs = await conn.list_issue_events("12345")
        assert len(docs) == 1
        assert docs[0].metadata["issue_id"] == "12345"
        assert docs[0].tenant_id == TENANT_ID

    async def test_list_issue_events_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_issue_events = AsyncMock(return_value=[])
        docs = await conn.list_issue_events("12345")
        assert docs == []


# ══════════════════════════════════════════════════════════════════════════════
# Connector constants & init (3 extra tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestConnectorInit:
    def test_connector_type_and_auth_type(self) -> None:
        from connector import AUTH_TYPE, CONNECTOR_TYPE
        assert CONNECTOR_TYPE == "sentry"
        assert AUTH_TYPE == "api_key"

    def test_connector_sets_config_fields(self) -> None:
        conn = _make_connector()
        assert conn._auth_token == AUTH_TOKEN
        assert conn._org_slug == ORG_SLUG
        assert conn.tenant_id == TENANT_ID
        assert conn.connector_id == CONNECTOR_ID

    def test_connector_default_empty_config(self) -> None:
        conn = SentryConnector()
        assert conn._auth_token == ""
        assert conn._org_slug == ""
        missing = conn._missing_credentials()
        assert "auth_token" in missing
        assert "organization_slug" in missing

    async def test_context_manager(self) -> None:
        conn = _make_connector()
        async with conn as c:
            assert c is conn
