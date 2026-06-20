"""Unit tests for BugsnagConnector — all HTTP calls are mocked via AsyncMock.

Coverage targets:
  exceptions        (5+)
  models            (6+)
  normalize_*       (9+)
  with_retry        (6+)
  HTTP client       (15+)
  install           (5+)
  health_check      (5+)
  sync              (8+)
  list_*            (8+)
  get_error / 404   (4+)
  Total: 71+
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import BugsnagConnector
from exceptions import (
    BugsnagAuthError,
    BugsnagError,
    BugsnagNetworkError,
    BugsnagNotFoundError,
    BugsnagRateLimitError,
)
from helpers.utils import (
    normalize_error,
    normalize_project,
    normalize_release,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ──────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_bugsnag_test_001"
AUTH_TOKEN = "bugsnag_test_personal_auth_token_abc123"
ORG_SLUG = "my-bugsnag-org"
PROJECT_ID = "proj-bugsnag-001"

SAMPLE_ORG: dict = {
    "id": "org-001",
    "slug": ORG_SLUG,
    "name": "My Bugsnag Organization",
    "created_at": "2026-01-01T00:00:00.000Z",
}

SAMPLE_PROJECT: dict = {
    "id": PROJECT_ID,
    "name": "My iOS App",
    "slug": "my-ios-app",
    "language": "swift",
    "created_at": "2026-01-15T10:00:00.000Z",
    "updated_at": "2026-06-01T12:00:00.000Z",
    "url": "https://api.bugsnag.com/projects/proj-bugsnag-001",
    "html_url": "https://app.bugsnag.com/my-bugsnag-org/my-ios-app",
}

SAMPLE_ERROR: dict = {
    "id": "err-001",
    "error_class": "NullPointerException",
    "message": "Attempt to invoke virtual method on a null object",
    "severity": "error",
    "status": "open",
    "first_seen": "2026-06-01T08:00:00.000Z",
    "last_seen": "2026-06-19T14:00:00.000Z",
    "events": 157,
    "users": 42,
    "url": "https://app.bugsnag.com/my-bugsnag-org/my-ios-app/errors/err-001",
}

SAMPLE_RELEASE: dict = {
    "id": "rel-001",
    "version": "2.5.1",
    "released_at": "2026-06-10T09:00:00.000Z",
    "release_stage": "production",
    "builder_name": "CI/CD Pipeline",
    "source_control_provider": "github",
    "source_control_revision": "abc1234def",
}

SAMPLE_COLLABORATOR: dict = {
    "id": "collab-001",
    "email": "alice@example.com",
    "name": "Alice Smith",
}


def _make_connector(
    auth_token: str = AUTH_TOKEN,
    org_slug: str = ORG_SLUG,
) -> BugsnagConnector:
    return BugsnagConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "auth_token": auth_token,
            "organization_slug": org_slug,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# Exceptions (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_bugsnag_error_base(self) -> None:
        exc = BugsnagError("base error", status_code=400, code="bad_request")
        assert str(exc) == "base error"
        assert exc.status_code == 400
        assert exc.code == "bad_request"

    def test_bugsnag_auth_error_is_bugsnag_error(self) -> None:
        exc = BugsnagAuthError("auth failed", status_code=401, code="auth_error")
        assert isinstance(exc, BugsnagError)
        assert exc.status_code == 401

    def test_bugsnag_rate_limit_error_defaults(self) -> None:
        exc = BugsnagRateLimitError("rate limited")
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 0.0

    def test_bugsnag_not_found_error_message(self) -> None:
        exc = BugsnagNotFoundError("error", "err-999")
        assert "err-999" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"

    def test_bugsnag_network_error_is_bugsnag_error(self) -> None:
        exc = BugsnagNetworkError("timeout", status_code=504)
        assert isinstance(exc, BugsnagError)
        assert exc.status_code == 504


# ══════════════════════════════════════════════════════════════════════════════
# Models (6 tests)
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

    def test_health_check_result_fields(self) -> None:
        from models import HealthCheckResult
        r = HealthCheckResult(
            health=ConnectorHealth.DEGRADED,
            auth_status=AuthStatus.FAILED,
            message="Network error",
        )
        assert r.health == ConnectorHealth.DEGRADED
        assert r.message == "Network error"

    def test_connector_document_fields(self) -> None:
        from models import ConnectorDocument
        doc = ConnectorDocument(
            source_id="abc123def456abcd",
            title="NullPointerException",
            content="Error class: NullPointerException",
            connector_id="conn1",
            tenant_id="tenant1",
            source_url="https://app.bugsnag.com/error/1",
            metadata={"severity": "error"},
        )
        assert doc.source_id == "abc123def456abcd"
        assert doc.metadata["severity"] == "error"


# ══════════════════════════════════════════════════════════════════════════════
# normalize_error (4 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestNormalizeError:
    def test_basic_fields(self) -> None:
        doc = normalize_error(SAMPLE_ERROR, PROJECT_ID)
        assert "NullPointerException" in doc.title
        assert "Attempt to invoke" in doc.title
        assert "error" in doc.content.lower()
        assert doc.source_url == SAMPLE_ERROR["url"]

    def test_source_id_is_16_chars_hex(self) -> None:
        doc = normalize_error(SAMPLE_ERROR, PROJECT_ID)
        assert len(doc.source_id) == 16
        assert all(c in "0123456789abcdef" for c in doc.source_id)

    def test_metadata_contains_all_fields(self) -> None:
        doc = normalize_error(SAMPLE_ERROR, PROJECT_ID)
        assert doc.metadata["error_id"] == "err-001"
        assert doc.metadata["error_class"] == "NullPointerException"
        assert doc.metadata["severity"] == "error"
        assert doc.metadata["status"] == "open"
        assert doc.metadata["project_id"] == PROJECT_ID
        assert doc.metadata["events_count"] == 157
        assert doc.metadata["users_count"] == 42
        assert doc.metadata["first_seen"] == SAMPLE_ERROR["first_seen"]
        assert doc.metadata["last_seen"] == SAMPLE_ERROR["last_seen"]

    def test_source_id_stable(self) -> None:
        doc1 = normalize_error(SAMPLE_ERROR, PROJECT_ID)
        doc2 = normalize_error(SAMPLE_ERROR, PROJECT_ID)
        assert doc1.source_id == doc2.source_id

    def test_empty_error_fallback_title(self) -> None:
        doc = normalize_error({})
        assert "Error" in doc.title
        assert len(doc.source_id) == 16

    def test_severity_in_metadata(self) -> None:
        doc = normalize_error({"id": "e1", "severity": "warning", "error_class": "TypeError"})
        assert doc.metadata["severity"] == "warning"


# ══════════════════════════════════════════════════════════════════════════════
# normalize_project (3 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestNormalizeProject:
    def test_basic_fields(self) -> None:
        doc = normalize_project(SAMPLE_PROJECT)
        assert doc.title == "My iOS App"
        assert "swift" in doc.content.lower()
        assert doc.source_url == SAMPLE_PROJECT["html_url"]

    def test_source_id_16_chars(self) -> None:
        doc = normalize_project(SAMPLE_PROJECT)
        assert len(doc.source_id) == 16

    def test_metadata_fields(self) -> None:
        doc = normalize_project(SAMPLE_PROJECT)
        assert doc.metadata["project_id"] == PROJECT_ID
        assert doc.metadata["slug"] == "my-ios-app"
        assert doc.metadata["language"] == "swift"


# ══════════════════════════════════════════════════════════════════════════════
# normalize_release (3 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestNormalizeRelease:
    def test_basic_fields(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE)
        assert "2.5.1" in doc.title
        assert "production" in doc.content.lower()

    def test_source_id_16_chars(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE)
        assert len(doc.source_id) == 16

    def test_source_id_stable_different_version(self) -> None:
        doc1 = normalize_release(SAMPLE_RELEASE)
        doc2 = normalize_release({**SAMPLE_RELEASE, "version": "2.5.2", "id": "rel-002"})
        assert doc1.source_id != doc2.source_id

    def test_metadata_fields(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE)
        assert doc.metadata["version"] == "2.5.1"
        assert doc.metadata["release_stage"] == "production"
        assert doc.metadata["builder_name"] == "CI/CD Pipeline"
        assert doc.metadata["source_control_provider"] == "github"


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
            side_effect=[BugsnagNetworkError("timeout"), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=BugsnagAuthError("bad token"))
        with pytest.raises(BugsnagAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    async def test_raises_after_max_attempts(self) -> None:
        err = BugsnagNetworkError("persistent error")
        fn = AsyncMock(side_effect=err)
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(BugsnagNetworkError):
                await with_retry(fn, max_attempts=3)
        assert fn.call_count == 3

    async def test_rate_limit_retry(self) -> None:
        fn = AsyncMock(
            side_effect=[
                BugsnagRateLimitError("rate limited", retry_after=0.0),
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
# HTTP client (mocked) (15 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestBugsnagHTTPClient:
    def _make_client(self) -> "BugsnagHTTPClient":
        from client.http_client import BugsnagHTTPClient
        return BugsnagHTTPClient(
            config={"auth_token": AUTH_TOKEN}
        )

    def test_token_auth_header_not_bearer(self) -> None:
        """Bugsnag uses 'token {auth_token}' NOT 'Bearer {auth_token}'."""
        client = self._make_client()
        headers = client._make_headers()
        assert headers["Authorization"] == f"token {AUTH_TOKEN}"
        assert "Bearer" not in headers["Authorization"]

    def test_base_url_default(self) -> None:
        client = self._make_client()
        assert client._base_url == "https://api.bugsnag.com"

    def test_base_url_strips_trailing_slash(self) -> None:
        from client.http_client import BugsnagHTTPClient
        client = BugsnagHTTPClient(config={"auth_token": AUTH_TOKEN, "base_url": "https://api.bugsnag.com/"})
        assert client._base_url == "https://api.bugsnag.com"

    async def test_get_organizations_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=([SAMPLE_ORG], {}))
        result = await client.get_organizations()
        assert len(result) == 1
        assert result[0]["slug"] == ORG_SLUG
        client._request.assert_called_once_with("GET", "/user/organizations")

    async def test_get_organization_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=(SAMPLE_ORG, {}))
        result = await client.get_organization(ORG_SLUG)
        assert result["name"] == "My Bugsnag Organization"
        client._request.assert_called_once_with("GET", f"/organizations/{ORG_SLUG}")

    async def test_get_projects_org_slug_in_url(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(
            return_value=([SAMPLE_PROJECT], {})
        )
        items, next_url = await client.get_projects(ORG_SLUG)
        assert len(items) == 1
        call_args = client._request.call_args
        assert ORG_SLUG in call_args[0][1]  # URL contains org slug

    async def test_get_projects_no_next_page(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(
            return_value=([SAMPLE_PROJECT], {})
        )
        items, next_url = await client.get_projects(ORG_SLUG)
        assert next_url is None

    async def test_get_projects_with_next_page_link(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(
            return_value=(
                [SAMPLE_PROJECT],
                {"X-Next-Page-Link": "https://api.bugsnag.com/organizations/my-bugsnag-org/projects?offset=100"},
            )
        )
        items, next_url = await client.get_projects(ORG_SLUG)
        assert next_url is not None
        assert "offset=100" in next_url

    async def test_get_errors_project_id_in_url(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(
            return_value=([SAMPLE_ERROR], {})
        )
        items, next_url = await client.get_errors(PROJECT_ID)
        call_args = client._request.call_args
        assert PROJECT_ID in call_args[0][1]

    async def test_get_errors_severity_filter(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(
            return_value=([SAMPLE_ERROR], {})
        )
        await client.get_errors(PROJECT_ID, severity="error")
        call_kwargs = client._request.call_args[1]
        params = call_kwargs.get("params", {})
        assert params.get("filters[severity]") == "error"

    async def test_get_error_single(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=(SAMPLE_ERROR, {}))
        result = await client.get_error(PROJECT_ID, "err-001")
        assert result["id"] == "err-001"
        call_args = client._request.call_args
        assert "err-001" in call_args[0][1]
        assert PROJECT_ID in call_args[0][1]

    async def test_get_releases(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=([SAMPLE_RELEASE], {}))
        result = await client.get_releases(PROJECT_ID)
        assert len(result) == 1
        assert result[0]["version"] == "2.5.1"

    async def test_get_collaborators(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=([SAMPLE_COLLABORATOR], {}))
        result = await client.get_collaborators(ORG_SLUG)
        assert len(result) == 1
        assert result[0]["email"] == "alice@example.com"
        call_args = client._request.call_args
        assert ORG_SLUG in call_args[0][1]

    async def test_raise_for_status_401(self) -> None:
        client = self._make_client()
        with pytest.raises(BugsnagAuthError) as exc_info:
            client._raise_for_status(401, {"errors": ["Unauthorized"]})
        assert exc_info.value.status_code == 401

    async def test_raise_for_status_403(self) -> None:
        client = self._make_client()
        with pytest.raises(BugsnagAuthError) as exc_info:
            client._raise_for_status(403, {"errors": ["Forbidden"]})
        assert exc_info.value.status_code == 403

    async def test_raise_for_status_404(self) -> None:
        client = self._make_client()
        with pytest.raises(BugsnagNotFoundError):
            client._raise_for_status(404, {})

    async def test_raise_for_status_429(self) -> None:
        client = self._make_client()
        with pytest.raises(BugsnagRateLimitError):
            client._raise_for_status(429, {})

    async def test_raise_for_status_500(self) -> None:
        client = self._make_client()
        with pytest.raises(BugsnagNetworkError) as exc_info:
            client._raise_for_status(500, {"message": "Internal Server Error"})
        assert exc_info.value.status_code == 500


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
        assert "My Bugsnag Organization" in result.message

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
            side_effect=BugsnagAuthError("Invalid token", status_code=401)
        )
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        conn = _make_connector()
        conn.client.get_organization = AsyncMock(
            side_effect=BugsnagNetworkError("Connection refused")
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
        assert "My Bugsnag Organization" in result.message

    async def test_health_check_missing_credentials(self) -> None:
        conn = _make_connector(auth_token="", org_slug="")
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        conn = _make_connector()
        conn.client.get_organization = AsyncMock(
            side_effect=BugsnagAuthError("Forbidden", status_code=403)
        )
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        conn = _make_connector()
        conn.client.get_organization = AsyncMock(
            side_effect=BugsnagNetworkError("Timeout")
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
    def _patched_connector(self) -> BugsnagConnector:
        conn = _make_connector()
        doc_proj = normalize_project(SAMPLE_PROJECT)
        doc_proj.connector_id = CONNECTOR_ID
        doc_proj.tenant_id = TENANT_ID
        doc_error = normalize_error(SAMPLE_ERROR, PROJECT_ID)
        doc_error.connector_id = CONNECTOR_ID
        doc_error.tenant_id = TENANT_ID

        conn.list_projects = AsyncMock(return_value=[doc_proj])
        conn.list_errors = AsyncMock(return_value=[doc_error])
        return conn

    async def test_sync_completed_status(self) -> None:
        conn = self._patched_connector()
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_counts_projects_and_errors(self) -> None:
        conn = self._patched_connector()
        result = await conn.sync()
        # 1 project + 1 error = 2
        assert result.documents_found == 2
        assert result.documents_synced == 2
        assert result.documents_failed == 0

    async def test_sync_multiple_projects(self) -> None:
        conn = _make_connector()
        proj1 = normalize_project(SAMPLE_PROJECT)
        proj1.connector_id = CONNECTOR_ID
        proj1.tenant_id = TENANT_ID
        proj2 = normalize_project({**SAMPLE_PROJECT, "id": "proj-002", "name": "Android App"})
        proj2.connector_id = CONNECTOR_ID
        proj2.tenant_id = TENANT_ID

        doc_error = normalize_error(SAMPLE_ERROR, PROJECT_ID)
        doc_error.connector_id = CONNECTOR_ID
        doc_error.tenant_id = TENANT_ID

        conn.list_projects = AsyncMock(return_value=[proj1, proj2])
        conn.list_errors = AsyncMock(return_value=[doc_error])

        result = await conn.sync()
        # 2 projects + 2 error batches (1 error each)
        assert result.documents_found == 4
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_projects_failure_returns_failed(self) -> None:
        conn = _make_connector()
        conn.list_projects = AsyncMock(
            side_effect=BugsnagNetworkError("API down")
        )
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_partial_on_ingest_error(self) -> None:
        conn = _make_connector()
        doc_proj = normalize_project(SAMPLE_PROJECT)
        doc_proj.connector_id = CONNECTOR_ID
        doc_proj.tenant_id = TENANT_ID

        conn.list_projects = AsyncMock(return_value=[doc_proj])
        conn.list_errors = AsyncMock(return_value=[])

        async def _bad_ingest(doc: object, kb_id: str) -> None:
            raise RuntimeError("ingest failed")

        conn._ingest_document = _bad_ingest  # type: ignore[method-assign]
        result = await conn.sync(kb_id="kb123")
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed == 1

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        conn = _make_connector()
        doc_proj = normalize_project(SAMPLE_PROJECT)
        doc_proj.connector_id = CONNECTOR_ID
        doc_proj.tenant_id = TENANT_ID

        conn.list_projects = AsyncMock(return_value=[doc_proj])
        conn.list_errors = AsyncMock(return_value=[])
        conn._ingest_document = AsyncMock()

        await conn.sync(kb_id="kb123")
        conn._ingest_document.assert_called_once()

    async def test_sync_no_kb_id_skips_ingest(self) -> None:
        conn = self._patched_connector()
        conn._ingest_document = AsyncMock()
        await conn.sync(kb_id="")
        conn._ingest_document.assert_not_called()

    async def test_sync_errors_failure_is_non_fatal(self) -> None:
        """Error fetch failure per project is non-fatal — sync continues."""
        conn = _make_connector()
        doc_proj = normalize_project(SAMPLE_PROJECT)
        doc_proj.connector_id = CONNECTOR_ID
        doc_proj.tenant_id = TENANT_ID

        conn.list_projects = AsyncMock(return_value=[doc_proj])
        conn.list_errors = AsyncMock(
            side_effect=BugsnagNetworkError("errors API down")
        )
        result = await conn.sync()
        # project synced, error batch failed non-fatally
        assert result.documents_synced == 1
        assert result.status == SyncStatus.COMPLETED  # no failed count incremented


# ══════════════════════════════════════════════════════════════════════════════
# list_projects / list_errors / list_releases / list_collaborators (8 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestListMethods:
    async def test_list_projects_single_page(self) -> None:
        conn = _make_connector()
        conn.client.get_projects = AsyncMock(
            return_value=([SAMPLE_PROJECT], None)
        )
        docs = await conn.list_projects()
        assert len(docs) == 1
        assert docs[0].title == "My iOS App"
        assert docs[0].tenant_id == TENANT_ID
        assert docs[0].connector_id == CONNECTOR_ID

    async def test_list_projects_pagination_stops_on_no_next(self) -> None:
        conn = _make_connector()
        conn.client.get_projects = AsyncMock(
            side_effect=[
                ([SAMPLE_PROJECT], "https://api.bugsnag.com/organizations/my-org/projects?offset=100"),
                ([{**SAMPLE_PROJECT, "id": "proj-002"}], None),
            ]
        )
        docs = await conn.list_projects()
        assert len(docs) == 2

    async def test_list_errors_single_page(self) -> None:
        conn = _make_connector()
        conn.client.get_errors = AsyncMock(
            return_value=([SAMPLE_ERROR], None)
        )
        docs = await conn.list_errors(PROJECT_ID)
        assert len(docs) == 1
        assert docs[0].metadata["error_id"] == "err-001"
        assert docs[0].tenant_id == TENANT_ID

    async def test_list_errors_with_severity_filter(self) -> None:
        conn = _make_connector()
        conn.client.get_errors = AsyncMock(
            return_value=([SAMPLE_ERROR], None)
        )
        docs = await conn.list_errors(PROJECT_ID, severity="error")
        conn.client.get_errors.assert_called_once_with(
            PROJECT_ID,
            per_page=25,
            per_page_offset=None,
            severity="error",
        )
        assert len(docs) == 1

    async def test_list_errors_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_errors = AsyncMock(return_value=([], None))
        docs = await conn.list_errors(PROJECT_ID)
        assert docs == []

    async def test_list_releases_returns_docs(self) -> None:
        conn = _make_connector()
        conn.client.get_releases = AsyncMock(return_value=[SAMPLE_RELEASE])
        docs = await conn.list_releases(PROJECT_ID)
        assert len(docs) == 1
        assert "2.5.1" in docs[0].title
        assert docs[0].tenant_id == TENANT_ID
        assert docs[0].connector_id == CONNECTOR_ID

    async def test_list_releases_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_releases = AsyncMock(return_value=[])
        docs = await conn.list_releases(PROJECT_ID)
        assert docs == []

    async def test_list_collaborators_returns_raw(self) -> None:
        conn = _make_connector()
        conn.client.get_collaborators = AsyncMock(return_value=[SAMPLE_COLLABORATOR])
        result = await conn.list_collaborators()
        assert len(result) == 1
        assert result[0]["email"] == "alice@example.com"


# ══════════════════════════════════════════════════════════════════════════════
# get_error (4 tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestGetError:
    async def test_get_error_returns_raw_dict(self) -> None:
        conn = _make_connector()
        conn.client.get_error = AsyncMock(return_value=SAMPLE_ERROR)
        result = await conn.get_error(PROJECT_ID, "err-001")
        assert result["id"] == "err-001"
        assert result["error_class"] == "NullPointerException"

    async def test_get_error_not_found_raises(self) -> None:
        conn = _make_connector()
        conn.client.get_error = AsyncMock(
            side_effect=BugsnagNotFoundError("error", "err-999")
        )
        with pytest.raises(BugsnagNotFoundError):
            await conn.get_error(PROJECT_ID, "err-999")

    async def test_get_error_passes_correct_ids(self) -> None:
        conn = _make_connector()
        conn.client.get_error = AsyncMock(return_value=SAMPLE_ERROR)
        await conn.get_error(PROJECT_ID, "err-001")
        conn.client.get_error.assert_called_once_with(PROJECT_ID, "err-001")

    async def test_get_error_auth_failure_propagates(self) -> None:
        conn = _make_connector()
        conn.client.get_error = AsyncMock(
            side_effect=BugsnagAuthError("Invalid token")
        )
        with pytest.raises(BugsnagAuthError):
            await conn.get_error(PROJECT_ID, "err-001")


# ══════════════════════════════════════════════════════════════════════════════
# Connector constants & init (4 extra tests)
# ══════════════════════════════════════════════════════════════════════════════


class TestConnectorInit:
    def test_connector_type_and_auth_type(self) -> None:
        from connector import AUTH_TYPE, CONNECTOR_TYPE
        assert CONNECTOR_TYPE == "bugsnag"
        assert AUTH_TYPE == "api_key"

    def test_connector_sets_config_fields(self) -> None:
        conn = _make_connector()
        assert conn._auth_token == AUTH_TOKEN
        assert conn._org_slug == ORG_SLUG
        assert conn.tenant_id == TENANT_ID
        assert conn.connector_id == CONNECTOR_ID

    def test_connector_default_empty_config(self) -> None:
        conn = BugsnagConnector()
        assert conn._auth_token == ""
        assert conn._org_slug == ""
        missing = conn._missing_credentials()
        assert "auth_token" in missing
        assert "organization_slug" in missing

    async def test_context_manager(self) -> None:
        conn = _make_connector()
        async with conn as c:
            assert c is conn
