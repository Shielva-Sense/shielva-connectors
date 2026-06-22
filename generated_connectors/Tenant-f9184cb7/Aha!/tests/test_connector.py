"""Unit tests for AhaConnector — all HTTP calls are mocked."""
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

from connector import AhaConnector, AUTH_TYPE, CONNECTOR_TYPE
from exceptions import (
    AhaAuthError,
    AhaError,
    AhaNetworkError,
    AhaNotFoundError,
    AhaRateLimitError,
)
from helpers.utils import (
    normalize_feature,
    normalize_goal,
    normalize_idea,
    normalize_release,
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

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_aha_test_001"
API_KEY = "test_aha_api_key_abc123"
SUBDOMAIN = "mycompany"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_ME: dict[str, Any] = {
    "user": {
        "id": "user_001",
        "name": "Alice Roadmap",
        "email": "alice@mycompany.com",
    }
}

SAMPLE_ME_NO_NAME: dict[str, Any] = {
    "user": {
        "id": "user_002",
        "email": "bob@mycompany.com",
    }
}

SAMPLE_PRODUCT: dict[str, Any] = {
    "id": "PROD_001",
    "reference_prefix": "MYAPP",
    "name": "My App",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z",
}

SAMPLE_PRODUCT_2: dict[str, Any] = {
    "id": "PROD_002",
    "reference_prefix": "BETA",
    "name": "Beta Product",
    "created_at": "2026-02-01T00:00:00Z",
    "updated_at": "2026-06-10T00:00:00Z",
}

SAMPLE_FEATURE: dict[str, Any] = {
    "id": "FEAT_001",
    "reference_num": "MYAPP-1",
    "name": "Dark mode support",
    "description": {"body": "Add dark mode to all screens."},
    "workflow_status": {"name": "In development"},
    "release": {"reference_num": "MYAPP-R-1"},
    "url": "https://mycompany.aha.io/features/MYAPP-1",
    "created_at": "2026-03-01T00:00:00Z",
    "updated_at": "2026-06-05T00:00:00Z",
}

SAMPLE_FEATURE_2: dict[str, Any] = {
    "id": "FEAT_002",
    "reference_num": "MYAPP-2",
    "name": "API rate limiting",
    "description": None,
    "workflow_status": None,
    "release": None,
    "url": "https://mycompany.aha.io/features/MYAPP-2",
    "created_at": "2026-04-01T00:00:00Z",
    "updated_at": "2026-06-10T00:00:00Z",
}

SAMPLE_RELEASE: dict[str, Any] = {
    "id": "REL_001",
    "reference_num": "MYAPP-R-1",
    "name": "Q3 2026 Release",
    "release_date": "2026-09-30",
    "url": "https://mycompany.aha.io/releases/MYAPP-R-1",
    "created_at": "2026-01-15T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z",
}

SAMPLE_IDEA: dict[str, Any] = {
    "id": "IDEA_001",
    "reference_num": "MYAPP-I-1",
    "name": "Support SSO login",
    "description": {"body": "Users want SAML SSO support."},
    "workflow_status": {"name": "Under review"},
    "votes_count": 42,
    "url": "https://mycompany.aha.io/ideas/MYAPP-I-1",
    "created_at": "2026-02-10T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z",
}

SAMPLE_GOAL: dict[str, Any] = {
    "id": "GOAL_001",
    "reference_num": "MYAPP-G-1",
    "name": "Grow user base to 10k",
    "description": {"body": "Reach 10,000 active users by end of year."},
    "url": "https://mycompany.aha.io/goals/MYAPP-G-1",
    "created_at": "2026-01-05T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z",
}

SINGLE_PAGE_PAGINATION: dict[str, Any] = {"total_pages": 1, "current_page": 1}


def _make_connector(
    api_key: str = API_KEY,
    subdomain: str = SUBDOMAIN,
) -> AhaConnector:
    return AhaConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key, "subdomain": subdomain},
    )


def _short_id(prefix: str, value: str) -> str:
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════════════════════
# 1 — Exception classes
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_aha_error_base(self) -> None:
        exc = AhaError("something broke", status_code=500, code="server_error")
        assert str(exc) == "something broke"
        assert exc.message == "something broke"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_aha_error_defaults(self) -> None:
        exc = AhaError("minimal")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_aha_auth_error_is_aha_error(self) -> None:
        exc = AhaAuthError("auth failed", status_code=401)
        assert isinstance(exc, AhaError)
        assert exc.status_code == 401

    def test_aha_rate_limit_error(self) -> None:
        exc = AhaRateLimitError("too many requests", retry_after=30.0)
        assert isinstance(exc, AhaError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 30.0

    def test_aha_rate_limit_default_retry_after(self) -> None:
        exc = AhaRateLimitError("rate limited")
        assert exc.retry_after == 0.0

    def test_aha_not_found_error(self) -> None:
        exc = AhaNotFoundError("feature", "FEAT_999")
        assert isinstance(exc, AhaError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "FEAT_999" in str(exc)
        assert "feature" in str(exc)

    def test_aha_network_error_is_aha_error(self) -> None:
        exc = AhaNetworkError("timeout")
        assert isinstance(exc, AhaError)
        assert str(exc) == "timeout"


# ═══════════════════════════════════════════════════════════════════════════════
# 2 — Models
# ═══════════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"
        assert SyncStatus.RUNNING == "running"

    def test_install_result_defaults(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
        )
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="OK",
        )
        assert r.message == "OK"

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test Feature",
            content="Feature: Test",
            connector_id=CONNECTOR_ID,
            tenant_id=TENANT_ID,
        )
        assert doc.source_url == ""
        assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 3 — Normalizers
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeFeature:
    def test_stable_source_id(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == _short_id("feature", "FEAT_001")
        assert len(doc.source_id) == 16

    def test_title(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Dark mode support"

    def test_content_includes_name(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert "Dark mode support" in doc.content

    def test_content_includes_reference(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert "MYAPP-1" in doc.content

    def test_content_includes_description(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert "Add dark mode" in doc.content

    def test_content_includes_status(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert "In development" in doc.content

    def test_source_url(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert "MYAPP-1" in doc.source_url

    def test_metadata_type(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["type"] == "feature"

    def test_metadata_feature_id(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["feature_id"] == "FEAT_001"

    def test_no_description(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE_2, CONNECTOR_ID, TENANT_ID)
        assert doc.title == "API rate limiting"
        assert "Description" not in doc.content

    def test_no_status(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE_2, CONNECTOR_ID, TENANT_ID)
        assert "Status" not in doc.content

    def test_fallback_title_on_empty_name(self) -> None:
        doc = normalize_feature({"id": "FEAT_X"}, CONNECTOR_ID, TENANT_ID)
        assert "FEAT_X" in doc.title

    def test_connector_id_and_tenant(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID


class TestNormalizeRelease:
    def test_stable_source_id(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == _short_id("release", "REL_001")

    def test_title(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE, CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Q3 2026 Release"

    def test_content_includes_release_date(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE, CONNECTOR_ID, TENANT_ID)
        assert "2026-09-30" in doc.content

    def test_metadata_type(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["type"] == "release"

    def test_metadata_release_id(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["release_id"] == "REL_001"

    def test_fallback_title_on_empty_name(self) -> None:
        doc = normalize_release({"id": "REL_X"}, CONNECTOR_ID, TENANT_ID)
        assert "REL_X" in doc.title

    def test_source_url(self) -> None:
        doc = normalize_release(SAMPLE_RELEASE, CONNECTOR_ID, TENANT_ID)
        assert "MYAPP-R-1" in doc.source_url


class TestNormalizeIdea:
    def test_stable_source_id(self) -> None:
        doc = normalize_idea(SAMPLE_IDEA, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == _short_id("idea", "IDEA_001")

    def test_title(self) -> None:
        doc = normalize_idea(SAMPLE_IDEA, CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Support SSO login"

    def test_content_includes_votes(self) -> None:
        doc = normalize_idea(SAMPLE_IDEA, CONNECTOR_ID, TENANT_ID)
        assert "42" in doc.content

    def test_content_includes_status(self) -> None:
        doc = normalize_idea(SAMPLE_IDEA, CONNECTOR_ID, TENANT_ID)
        assert "Under review" in doc.content

    def test_metadata_type(self) -> None:
        doc = normalize_idea(SAMPLE_IDEA, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["type"] == "idea"

    def test_metadata_votes(self) -> None:
        doc = normalize_idea(SAMPLE_IDEA, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["votes"] == 42

    def test_zero_votes_not_in_content(self) -> None:
        idea_no_votes = {**SAMPLE_IDEA, "votes_count": 0}
        doc = normalize_idea(idea_no_votes, CONNECTOR_ID, TENANT_ID)
        assert "Votes" not in doc.content

    def test_fallback_title_on_empty_name(self) -> None:
        doc = normalize_idea({"id": "IDEA_X"}, CONNECTOR_ID, TENANT_ID)
        assert "IDEA_X" in doc.title


class TestNormalizeGoal:
    def test_stable_source_id(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == _short_id("goal", "GOAL_001")

    def test_title(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Grow user base to 10k"

    def test_content_includes_description(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert "10,000 active users" in doc.content

    def test_metadata_type(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["type"] == "goal"

    def test_metadata_goal_id(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["goal_id"] == "GOAL_001"

    def test_fallback_title_on_empty_name(self) -> None:
        doc = normalize_goal({"id": "GOAL_X"}, CONNECTOR_ID, TENANT_ID)
        assert "GOAL_X" in doc.title

    def test_source_url(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert "MYAPP-G-1" in doc.source_url


# ═══════════════════════════════════════════════════════════════════════════════
# 4 — with_retry
# ═══════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    async def test_success_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, "arg1", max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_success_after_network_retry(self) -> None:
        fn = AsyncMock(
            side_effect=[AhaNetworkError("timeout"), {"ok": True}]
        )
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=AhaAuthError("bad key"))
        with pytest.raises(AhaAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 1

    async def test_exhausted_attempts_raises(self) -> None:
        fn = AsyncMock(side_effect=AhaNetworkError("timeout"))
        with pytest.raises(AhaNetworkError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    async def test_rate_limit_retried(self) -> None:
        fn = AsyncMock(
            side_effect=[
                AhaRateLimitError("rate limited", retry_after=0),
                {"ok": True},
            ]
        )
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 5 — AhaHTTPClient
# ═══════════════════════════════════════════════════════════════════════════════


class TestAhaHTTPClient:
    """Test the HTTP client with mocked aiohttp session."""

    def _make_mock_response(
        self,
        status: int,
        json_body: Any,
        headers: dict[str, str] | None = None,
    ) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.headers = headers or {}
        resp.json = AsyncMock(return_value=json_body)
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    def _patch_session(self, connector: Any, mock_resp: MagicMock) -> MagicMock:
        """Patch the underlying aiohttp session on the http_client."""
        from client.http_client import AhaHTTPClient

        client = AhaHTTPClient(subdomain=SUBDOMAIN)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.request = MagicMock(return_value=mock_resp)
        client._session = mock_session
        return client

    async def test_bearer_header_sent(self) -> None:
        from client.http_client import AhaHTTPClient

        client = AhaHTTPClient(subdomain=SUBDOMAIN)
        headers = client._headers(API_KEY)
        assert headers["Authorization"] == f"Bearer {API_KEY}"
        assert headers["Accept"] == "application/json"

    async def test_base_url_uses_subdomain(self) -> None:
        from client.http_client import AhaHTTPClient

        client = AhaHTTPClient(subdomain="acme")
        assert client._base_url == "https://acme.aha.io/api/v1"

    def _make_client_with_session(self, mock_resp: MagicMock) -> Any:
        from client.http_client import AhaHTTPClient

        client = AhaHTTPClient(subdomain=SUBDOMAIN)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        mock_session.request = MagicMock(return_value=mock_resp)
        client._session = mock_session
        return client

    async def test_get_me_success(self) -> None:
        mock_resp = self._make_mock_response(200, SAMPLE_ME)
        client = self._make_client_with_session(mock_resp)
        result = await client.get_me(API_KEY)
        assert result == SAMPLE_ME
        await client.aclose()

    async def test_get_products_success(self) -> None:
        body = {"products": [SAMPLE_PRODUCT], "pagination": SINGLE_PAGE_PAGINATION}
        mock_resp = self._make_mock_response(200, body)
        client = self._make_client_with_session(mock_resp)
        result = await client.get_products(API_KEY, page=1)
        assert result["products"][0]["id"] == "PROD_001"
        await client.aclose()

    async def test_get_features_success(self) -> None:
        body = {"features": [SAMPLE_FEATURE], "pagination": SINGLE_PAGE_PAGINATION}
        mock_resp = self._make_mock_response(200, body)
        client = self._make_client_with_session(mock_resp)
        result = await client.get_features(API_KEY, "PROD_001", page=1)
        assert result["features"][0]["id"] == "FEAT_001"
        await client.aclose()

    async def test_get_feature_single(self) -> None:
        body = {"feature": SAMPLE_FEATURE}
        mock_resp = self._make_mock_response(200, body)
        client = self._make_client_with_session(mock_resp)
        result = await client.get_feature(API_KEY, "FEAT_001")
        assert result == body
        await client.aclose()

    async def test_get_goals_success(self) -> None:
        body = {"goals": [SAMPLE_GOAL]}
        mock_resp = self._make_mock_response(200, body)
        client = self._make_client_with_session(mock_resp)
        result = await client.get_goals(API_KEY, "PROD_001")
        assert result["goals"][0]["id"] == "GOAL_001"
        await client.aclose()

    async def test_get_releases_success(self) -> None:
        body = {"releases": [SAMPLE_RELEASE], "pagination": SINGLE_PAGE_PAGINATION}
        mock_resp = self._make_mock_response(200, body)
        client = self._make_client_with_session(mock_resp)
        result = await client.get_releases(API_KEY, "PROD_001", page=1)
        assert result["releases"][0]["id"] == "REL_001"
        await client.aclose()

    async def test_get_ideas_success(self) -> None:
        body = {"ideas": [SAMPLE_IDEA], "pagination": SINGLE_PAGE_PAGINATION}
        mock_resp = self._make_mock_response(200, body)
        client = self._make_client_with_session(mock_resp)
        result = await client.get_ideas(API_KEY, "PROD_001", page=1)
        assert result["ideas"][0]["id"] == "IDEA_001"
        await client.aclose()

    async def test_raise_for_status_401(self) -> None:
        mock_resp = self._make_mock_response(401, {"errors": ["Invalid API key"]})
        client = self._make_client_with_session(mock_resp)
        with pytest.raises(AhaAuthError):
            await client.get_me(API_KEY)
        await client.aclose()

    async def test_raise_for_status_403(self) -> None:
        mock_resp = self._make_mock_response(403, {"errors": ["Forbidden"]})
        client = self._make_client_with_session(mock_resp)
        with pytest.raises(AhaAuthError):
            await client.get_me(API_KEY)
        await client.aclose()

    async def test_raise_for_status_404(self) -> None:
        mock_resp = self._make_mock_response(404, {})
        client = self._make_client_with_session(mock_resp)
        with pytest.raises(AhaNotFoundError):
            await client.get_feature(API_KEY, "FEAT_999")
        await client.aclose()

    async def test_raise_for_status_429(self) -> None:
        mock_resp = self._make_mock_response(
            429, {}, headers={"Retry-After": "60"}
        )
        client = self._make_client_with_session(mock_resp)
        with pytest.raises(AhaRateLimitError) as exc_info:
            await client.get_me(API_KEY)
        assert exc_info.value.retry_after == 60.0
        await client.aclose()

    async def test_raise_for_status_500(self) -> None:
        mock_resp = self._make_mock_response(500, {"error": "Server error"})
        client = self._make_client_with_session(mock_resp)
        with pytest.raises(AhaNetworkError):
            await client.get_me(API_KEY)
        await client.aclose()

    async def test_aclose_clears_session(self) -> None:
        from client.http_client import AhaHTTPClient

        client = AhaHTTPClient(subdomain=SUBDOMAIN)
        mock_session = AsyncMock()
        mock_session.closed = False
        client._session = mock_session
        await client.aclose()
        assert client._session is None

    async def test_aclose_idempotent(self) -> None:
        from client.http_client import AhaHTTPClient

        client = AhaHTTPClient(subdomain=SUBDOMAIN)
        await client.aclose()  # no session — must not raise
        await client.aclose()


# ═══════════════════════════════════════════════════════════════════════════════
# 6 — install()
# ═══════════════════════════════════════════════════════════════════════════════


class TestInstall:
    async def test_install_success(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_me = AsyncMock(return_value=SAMPLE_ME)
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Alice Roadmap" in result.message

    async def test_install_success_email_fallback(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_me = AsyncMock(return_value=SAMPLE_ME_NO_NAME)
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert "bob@mycompany.com" in result.message

    async def test_install_missing_api_key(self) -> None:
        conn = _make_connector(api_key="")
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_missing_subdomain(self) -> None:
        conn = _make_connector(subdomain="")
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "subdomain" in result.message

    async def test_install_auth_error(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_me = AsyncMock(side_effect=AhaAuthError("bad key"))
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_me = AsyncMock(side_effect=AhaNetworkError("timeout"))
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 7 — health_check()
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    async def test_healthy(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_me = AsyncMock(return_value=SAMPLE_ME)
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Alice Roadmap" in result.message

    async def test_missing_creds(self) -> None:
        conn = _make_connector(api_key="")
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_auth_error(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_me = AsyncMock(side_effect=AhaAuthError("401"))
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_network_error(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_me = AsyncMock(side_effect=AhaNetworkError("timeout"))
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_generic_error(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_me = AsyncMock(side_effect=RuntimeError("unexpected"))
        mock_client.aclose = AsyncMock()
        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 8 — sync()
# ═══════════════════════════════════════════════════════════════════════════════


def _make_products_resp(products: list, total_pages: int = 1) -> dict:
    return {"products": products, "pagination": {"total_pages": total_pages, "current_page": 1}}


def _make_features_resp(features: list, total_pages: int = 1) -> dict:
    return {"features": features, "pagination": {"total_pages": total_pages, "current_page": 1}}


def _make_releases_resp(releases: list, total_pages: int = 1) -> dict:
    return {"releases": releases, "pagination": {"total_pages": total_pages, "current_page": 1}}


def _make_ideas_resp(ideas: list, total_pages: int = 1) -> dict:
    return {"ideas": ideas, "pagination": {"total_pages": total_pages, "current_page": 1}}


class TestSync:
    async def test_sync_returns_sync_result(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(return_value=_make_products_resp([]))
        mock_client.aclose = AsyncMock()
        conn._http_client = mock_client
        result = await conn.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_no_products(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(return_value=_make_products_resp([]))
        conn._http_client = mock_client
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_counts_features(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(
            return_value=_make_products_resp([SAMPLE_PRODUCT])
        )
        mock_client.get_features = AsyncMock(
            return_value=_make_features_resp([SAMPLE_FEATURE, SAMPLE_FEATURE_2])
        )
        mock_client.get_releases = AsyncMock(return_value=_make_releases_resp([]))
        mock_client.get_ideas = AsyncMock(return_value=_make_ideas_resp([]))
        conn._http_client = mock_client
        result = await conn.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_counts_releases(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(
            return_value=_make_products_resp([SAMPLE_PRODUCT])
        )
        mock_client.get_features = AsyncMock(return_value=_make_features_resp([]))
        mock_client.get_releases = AsyncMock(
            return_value=_make_releases_resp([SAMPLE_RELEASE])
        )
        mock_client.get_ideas = AsyncMock(return_value=_make_ideas_resp([]))
        conn._http_client = mock_client
        result = await conn.sync()
        assert result.documents_found == 1
        assert result.documents_synced == 1

    async def test_sync_counts_ideas(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(
            return_value=_make_products_resp([SAMPLE_PRODUCT])
        )
        mock_client.get_features = AsyncMock(return_value=_make_features_resp([]))
        mock_client.get_releases = AsyncMock(return_value=_make_releases_resp([]))
        mock_client.get_ideas = AsyncMock(
            return_value=_make_ideas_resp([SAMPLE_IDEA])
        )
        conn._http_client = mock_client
        result = await conn.sync()
        assert result.documents_found == 1
        assert result.documents_synced == 1

    async def test_sync_combined_all_types(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(
            return_value=_make_products_resp([SAMPLE_PRODUCT])
        )
        mock_client.get_features = AsyncMock(
            return_value=_make_features_resp([SAMPLE_FEATURE])
        )
        mock_client.get_releases = AsyncMock(
            return_value=_make_releases_resp([SAMPLE_RELEASE])
        )
        mock_client.get_ideas = AsyncMock(
            return_value=_make_ideas_resp([SAMPLE_IDEA])
        )
        conn._http_client = mock_client
        result = await conn.sync()
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_product_list_failure(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(side_effect=AhaNetworkError("fail"))
        conn._http_client = mock_client
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED
        assert "Failed to list products" in result.message

    async def test_sync_feature_error_is_partial(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(
            return_value=_make_products_resp([SAMPLE_PRODUCT])
        )
        mock_client.get_features = AsyncMock(side_effect=AhaNetworkError("fail"))
        mock_client.get_releases = AsyncMock(return_value=_make_releases_resp([]))
        mock_client.get_ideas = AsyncMock(return_value=_make_ideas_resp([]))
        conn._http_client = mock_client
        result = await conn.sync()
        # Features errored but the loop continues; other resources may still sync
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    async def test_sync_ingest_called_with_kb_id(self) -> None:
        conn = _make_connector()
        ingest_mock = AsyncMock()
        conn._ingest_document = ingest_mock
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(
            return_value=_make_products_resp([SAMPLE_PRODUCT])
        )
        mock_client.get_features = AsyncMock(
            return_value=_make_features_resp([SAMPLE_FEATURE])
        )
        mock_client.get_releases = AsyncMock(return_value=_make_releases_resp([]))
        mock_client.get_ideas = AsyncMock(return_value=_make_ideas_resp([]))
        conn._http_client = mock_client
        await conn.sync(kb_id="kb_test_001")
        ingest_mock.assert_called_once()
        doc_arg, kb_arg = ingest_mock.call_args.args
        assert isinstance(doc_arg, ConnectorDocument)
        assert kb_arg == "kb_test_001"

    async def test_sync_skips_product_without_id(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(
            return_value=_make_products_resp([{"name": "No ID"}])
        )
        conn._http_client = mock_client
        result = await conn.sync()
        assert result.documents_found == 0

    async def test_sync_multiple_products(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()

        products_resp = _make_products_resp([SAMPLE_PRODUCT, SAMPLE_PRODUCT_2])
        mock_client.get_products = AsyncMock(return_value=products_resp)
        # Each product gets 1 feature
        mock_client.get_features = AsyncMock(
            return_value=_make_features_resp([SAMPLE_FEATURE])
        )
        mock_client.get_releases = AsyncMock(return_value=_make_releases_resp([]))
        mock_client.get_ideas = AsyncMock(return_value=_make_ideas_resp([]))
        conn._http_client = mock_client
        result = await conn.sync()
        # 1 feature × 2 products = 2
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_feature_pagination(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(
            return_value=_make_products_resp([SAMPLE_PRODUCT])
        )
        # Page 1 returns 1 feature, page 2 returns 1 more
        page1 = {"features": [SAMPLE_FEATURE], "pagination": {"total_pages": 2, "current_page": 1}}
        page2 = {"features": [SAMPLE_FEATURE_2], "pagination": {"total_pages": 2, "current_page": 2}}
        mock_client.get_features = AsyncMock(side_effect=[page1, page2])
        mock_client.get_releases = AsyncMock(return_value=_make_releases_resp([]))
        mock_client.get_ideas = AsyncMock(return_value=_make_ideas_resp([]))
        conn._http_client = mock_client
        result = await conn.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 9 — list_products / list_features / list_releases / list_ideas / list_goals
# ═══════════════════════════════════════════════════════════════════════════════


class TestListMethods:
    async def test_list_products_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(
            return_value=_make_products_resp([SAMPLE_PRODUCT])
        )
        conn._http_client = mock_client
        products = await conn.list_products()
        assert isinstance(products, list)
        assert len(products) == 1
        assert products[0]["id"] == "PROD_001"

    async def test_list_products_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_products = AsyncMock(return_value=_make_products_resp([]))
        conn._http_client = mock_client
        products = await conn.list_products()
        assert products == []

    async def test_list_products_pagination(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        page1 = {"products": [SAMPLE_PRODUCT], "pagination": {"total_pages": 2}}
        page2 = {"products": [SAMPLE_PRODUCT_2], "pagination": {"total_pages": 2}}
        mock_client.get_products = AsyncMock(side_effect=[page1, page2])
        conn._http_client = mock_client
        products = await conn.list_products()
        assert len(products) == 2

    async def test_list_features_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_features = AsyncMock(
            return_value=_make_features_resp([SAMPLE_FEATURE])
        )
        conn._http_client = mock_client
        features = await conn.list_features("PROD_001")
        assert isinstance(features, list)
        assert len(features) == 1

    async def test_list_features_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_features = AsyncMock(return_value=_make_features_resp([]))
        conn._http_client = mock_client
        features = await conn.list_features("PROD_001")
        assert features == []

    async def test_list_releases_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_releases = AsyncMock(
            return_value=_make_releases_resp([SAMPLE_RELEASE])
        )
        conn._http_client = mock_client
        releases = await conn.list_releases("PROD_001")
        assert isinstance(releases, list)
        assert len(releases) == 1
        assert releases[0]["id"] == "REL_001"

    async def test_list_releases_pagination(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        r1 = SAMPLE_RELEASE.copy()
        r2 = {**SAMPLE_RELEASE, "id": "REL_002", "name": "Q4 Release"}
        page1 = {"releases": [r1], "pagination": {"total_pages": 2}}
        page2 = {"releases": [r2], "pagination": {"total_pages": 2}}
        mock_client.get_releases = AsyncMock(side_effect=[page1, page2])
        conn._http_client = mock_client
        releases = await conn.list_releases("PROD_001")
        assert len(releases) == 2

    async def test_list_ideas_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_ideas = AsyncMock(
            return_value=_make_ideas_resp([SAMPLE_IDEA])
        )
        conn._http_client = mock_client
        ideas = await conn.list_ideas("PROD_001")
        assert isinstance(ideas, list)
        assert len(ideas) == 1
        assert ideas[0]["id"] == "IDEA_001"

    async def test_list_ideas_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_ideas = AsyncMock(return_value=_make_ideas_resp([]))
        conn._http_client = mock_client
        ideas = await conn.list_ideas("PROD_001")
        assert ideas == []

    async def test_list_goals_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_goals = AsyncMock(return_value={"goals": [SAMPLE_GOAL]})
        conn._http_client = mock_client
        goals = await conn.list_goals("PROD_001")
        assert isinstance(goals, list)
        assert len(goals) == 1
        assert goals[0]["id"] == "GOAL_001"

    async def test_list_goals_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_goals = AsyncMock(return_value={"goals": []})
        conn._http_client = mock_client
        goals = await conn.list_goals("PROD_001")
        assert goals == []


# ═══════════════════════════════════════════════════════════════════════════════
# 10 — Constants & lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


class TestConstantsAndLifecycle:
    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "aha"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_class_connector_type(self) -> None:
        assert AhaConnector.CONNECTOR_TYPE == "aha"

    def test_class_auth_type(self) -> None:
        assert AhaConnector.AUTH_TYPE == "api_key"

    async def test_aclose_clears_http_client(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        conn._http_client = mock_client
        await conn.aclose()
        assert conn._http_client is None

    async def test_aclose_idempotent(self) -> None:
        conn = _make_connector()
        await conn.aclose()  # no client — must not raise
        await conn.aclose()

    async def test_context_manager(self) -> None:
        async with AhaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY, "subdomain": SUBDOMAIN},
        ) as conn:
            assert isinstance(conn, AhaConnector)
