"""Unit tests for LatticeConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AUTH_TYPE, CONNECTOR_TYPE, LatticeConnector
from exceptions import (
    LatticeAuthError,
    LatticeError,
    LatticeNetworkError,
    LatticeNotFoundError,
    LatticeRateLimitError,
)
from helpers.utils import normalize_goal, normalize_review, normalize_user, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

# ── Shared fixtures ───────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_lattice_test_001"
API_TOKEN = "lat_api_test_token_abc123"

SAMPLE_USER: dict = {
    "id": "u001",
    "firstName": "Jane",
    "lastName": "Smith",
    "displayName": "Jane Smith",
    "email": "jane.smith@acme.com",
    "jobTitle": "Software Engineer",
    "department": "Engineering",
    "status": "Active",
    "managerId": "u000",
    "startDate": "2023-03-15",
}

SAMPLE_USER_2: dict = {
    "id": "u002",
    "firstName": "Bob",
    "lastName": "Jones",
    "email": "bob.jones@acme.com",
    "jobTitle": "Product Manager",
    "department": "Product",
    "status": "Active",
    "managerId": "u000",
    "startDate": "2022-07-01",
}

SAMPLE_GOAL: dict = {
    "id": "g001",
    "name": "Increase customer NPS by 20%",
    "description": "Improve support flow to increase NPS score.",
    "status": "on_track",
    "progress": 45,
    "ownerId": "u001",
    "ownerName": "Jane Smith",
    "dueDate": "2026-12-31",
    "type": "company",
}

SAMPLE_REVIEW: dict = {
    "id": "r001",
    "title": "Q2 2026 Performance Review",
    "status": "complete",
    "score": "4.5",
    "revieweeId": "u001",
    "revieweeName": "Jane Smith",
    "reviewerId": "u000",
    "reviewerName": "Alice Manager",
    "period": "Q2 2026",
    "dueDate": "2026-06-30",
}

SAMPLE_FEEDBACK: dict = {
    "id": "f001",
    "message": "Great work on the product launch!",
    "giverId": "u000",
    "receiverId": "u001",
}

SAMPLE_DEPARTMENT: dict = {
    "id": "d001",
    "name": "Engineering",
}

ONE_PAGE_RESPONSE = {"data": [SAMPLE_USER], "meta": {"total_pages": 1, "total": 1}}
TWO_PAGE_RESPONSE_P1 = {
    "data": [SAMPLE_USER],
    "meta": {"total_pages": 2, "total": 2},
}
TWO_PAGE_RESPONSE_P2 = {
    "data": [SAMPLE_USER_2],
    "meta": {"total_pages": 2, "total": 2},
}
EMPTY_RESPONSE = {"data": [], "meta": {"total_pages": 0, "total": 0}}


def _make_connector(**kwargs: object) -> LatticeConnector:
    return LatticeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        api_token=API_TOKEN,
        **kwargs,  # type: ignore[arg-type]
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1 — Exception hierarchy
# ══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_lattice_error_is_exception(self) -> None:
        err = LatticeError("base error")
        assert isinstance(err, Exception)
        assert str(err) == "base error"

    def test_lattice_error_attributes(self) -> None:
        err = LatticeError("msg", status_code=400, code="bad_request")
        assert err.message == "msg"
        assert err.status_code == 400
        assert err.code == "bad_request"

    def test_lattice_error_defaults(self) -> None:
        err = LatticeError("plain")
        assert err.status_code == 0
        assert err.code == ""

    def test_auth_error_is_lattice_error(self) -> None:
        err = LatticeAuthError("unauthorized", status_code=401)
        assert isinstance(err, LatticeError)
        assert err.status_code == 401

    def test_network_error_is_lattice_error(self) -> None:
        err = LatticeNetworkError("timeout", status_code=504)
        assert isinstance(err, LatticeError)

    def test_not_found_error_message(self) -> None:
        err = LatticeNotFoundError("user", "u999")
        assert isinstance(err, LatticeError)
        assert "u999" in str(err)
        assert err.status_code == 404
        assert err.code == "resource_missing"

    def test_rate_limit_error_retry_after(self) -> None:
        err = LatticeRateLimitError("too many requests", retry_after=30.0)
        assert isinstance(err, LatticeError)
        assert err.retry_after == 30.0
        assert err.status_code == 429
        assert err.code == "rate_limit"

    def test_rate_limit_error_default_retry_after(self) -> None:
        err = LatticeRateLimitError("limited")
        assert err.retry_after == 0.0

    def test_not_found_error_is_lattice_error(self) -> None:
        err = LatticeNotFoundError("goal", "g404")
        assert isinstance(err, LatticeError)

    def test_auth_error_stores_code(self) -> None:
        err = LatticeAuthError("auth failed", status_code=403, code="auth_error")
        assert err.code == "auth_error"


# ══════════════════════════════════════════════════════════════════════════════
# 2 — Models
# ══════════════════════════════════════════════════════════════════════════════


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
        r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result(self) -> None:
        r = HealthCheckResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED, message="ok")
        assert r.message == "ok"

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="hello",
            connector_id="c1",
            tenant_id="t1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}

    def test_connector_document_metadata(self) -> None:
        doc = ConnectorDocument(
            source_id="abc",
            title="T",
            content="C",
            connector_id="c",
            tenant_id="t",
            metadata={"key": "val"},
        )
        assert doc.metadata["key"] == "val"


# ══════════════════════════════════════════════════════════════════════════════
# 3 — Normalizers
# ══════════════════════════════════════════════════════════════════════════════


class TestNormalizeUser:
    def test_returns_connector_document(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)

    def test_source_id_length(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16

    def test_source_id_is_hex(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        int(doc.source_id, 16)  # raises if not hex

    def test_source_id_is_sha256_prefix(self) -> None:
        expected = hashlib.sha256(f"user:{SAMPLE_USER['id']}".encode()).hexdigest()[:16]
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == expected

    def test_source_id_is_deterministic(self) -> None:
        doc1 = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id == doc2.source_id

    def test_different_users_different_ids(self) -> None:
        doc1 = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_user(SAMPLE_USER_2, CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id != doc2.source_id

    def test_title_contains_name(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert "Jane Smith" in doc.title

    def test_metadata_name(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["full_name"] == "Jane Smith"

    def test_metadata_department(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["department"] == "Engineering"

    def test_metadata_status(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["status"] == "Active"

    def test_metadata_email(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["email"] == "jane.smith@acme.com"

    def test_metadata_job_title(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["job_title"] == "Software Engineer"

    def test_content_includes_name(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert "Jane Smith" in doc.content

    def test_content_includes_department(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert "Engineering" in doc.content

    def test_document_type_is_employee(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert "Employee" in doc.title

    def test_fallback_name_from_first_last(self) -> None:
        user = {"id": "u003", "firstName": "Tom", "lastName": "Doe"}
        doc = normalize_user(user, CONNECTOR_ID, TENANT_ID)
        assert "Tom Doe" in doc.title

    def test_fallback_name_when_no_name(self) -> None:
        user = {"id": "u999"}
        doc = normalize_user(user, CONNECTOR_ID, TENANT_ID)
        assert "u999" in doc.title


class TestNormalizeGoal:
    def test_returns_connector_document(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)

    def test_source_id_length(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16

    def test_source_id_is_sha256_prefix(self) -> None:
        expected = hashlib.sha256(f"goal:{SAMPLE_GOAL['id']}".encode()).hexdigest()[:16]
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == expected

    def test_title_contains_goal_name(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert "Increase customer NPS" in doc.title

    def test_metadata_status(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["status"] == "on_track"

    def test_metadata_name(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert "Increase customer NPS" in doc.metadata["name"]

    def test_metadata_owner_name(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["owner_name"] == "Jane Smith"

    def test_metadata_progress(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["progress"] == "45"

    def test_content_includes_status(self) -> None:
        doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert "on_track" in doc.content

    def test_deterministic_id(self) -> None:
        doc1 = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id == doc2.source_id

    def test_fallback_name(self) -> None:
        goal = {"id": "g999"}
        doc = normalize_goal(goal, CONNECTOR_ID, TENANT_ID)
        assert "g999" in doc.title


class TestNormalizeReview:
    def test_returns_connector_document(self) -> None:
        doc = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)

    def test_source_id_length(self) -> None:
        doc = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16

    def test_source_id_is_sha256_prefix(self) -> None:
        expected = hashlib.sha256(f"review:{SAMPLE_REVIEW['id']}".encode()).hexdigest()[:16]
        doc = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == expected

    def test_title_contains_review_title(self) -> None:
        doc = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert "Q2 2026 Performance Review" in doc.title

    def test_metadata_status(self) -> None:
        doc = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["status"] == "complete"

    def test_metadata_score(self) -> None:
        doc = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["score"] == "4.5"

    def test_metadata_reviewee_name(self) -> None:
        doc = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["reviewee_name"] == "Jane Smith"

    def test_metadata_reviewer_name(self) -> None:
        doc = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["reviewer_name"] == "Alice Manager"

    def test_content_includes_status(self) -> None:
        doc = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert "complete" in doc.content

    def test_content_includes_reviewee(self) -> None:
        doc = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert "Jane Smith" in doc.content

    def test_deterministic_id(self) -> None:
        doc1 = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_review(SAMPLE_REVIEW, CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id == doc2.source_id


# ══════════════════════════════════════════════════════════════════════════════
# 4 — with_retry
# ══════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[LatticeNetworkError("timeout"), {"ok": True}]
        )
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=LatticeAuthError("401"))
        with pytest.raises(LatticeAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    async def test_raises_after_max_attempts_exhausted(self) -> None:
        err = LatticeNetworkError("persistent failure")
        fn = AsyncMock(side_effect=err)
        with pytest.raises(LatticeNetworkError):
            await with_retry(fn, max_attempts=2, base_delay=0)
        assert fn.call_count == 2

    async def test_rate_limit_error_retried(self) -> None:
        fn = AsyncMock(
            side_effect=[LatticeRateLimitError("429", retry_after=0), {"ok": True}]
        )
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_rate_limit_raises_after_max(self) -> None:
        fn = AsyncMock(side_effect=LatticeRateLimitError("429 always"))
        with pytest.raises(LatticeRateLimitError):
            await with_retry(fn, max_attempts=2, base_delay=0)


# ══════════════════════════════════════════════════════════════════════════════
# 5 — LatticeHTTPClient
# ══════════════════════════════════════════════════════════════════════════════


class TestLatticeHTTPClient:
    from client.http_client import LatticeHTTPClient

    def _client(self) -> "LatticeHTTPClient":
        from client.http_client import LatticeHTTPClient
        return LatticeHTTPClient(api_token=API_TOKEN)

    def test_bearer_header(self) -> None:
        client = self._client()
        headers = client._headers()
        assert headers["Authorization"] == f"Bearer {API_TOKEN}"

    def test_base_url_default(self) -> None:
        from client.http_client import LatticeHTTPClient, LATTICE_BASE_URL
        client = LatticeHTTPClient(api_token=API_TOKEN)
        assert client._base_url == LATTICE_BASE_URL.rstrip("/")

    async def test_get_users_calls_correct_path(self) -> None:
        client = self._client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = ONE_PAGE_RESPONSE
            await client.get_users(page=1, per_page=25)
            mock_req.assert_called_once_with(
                "GET", "/v1/users", params={"page": 1, "per_page": 25}
            )

    async def test_get_user_calls_correct_path(self) -> None:
        client = self._client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"data": SAMPLE_USER}
            await client.get_user("u001")
            mock_req.assert_called_once_with("GET", "/v1/users/u001")

    async def test_get_departments_calls_correct_path(self) -> None:
        client = self._client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"data": [SAMPLE_DEPARTMENT], "meta": {"total_pages": 1}}
            await client.get_departments(page=1)
            mock_req.assert_called_once_with(
                "GET", "/v1/departments", params={"page": 1}
            )

    async def test_get_goals_calls_correct_path(self) -> None:
        client = self._client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"data": [SAMPLE_GOAL], "meta": {"total_pages": 1}}
            await client.get_goals(page=1, per_page=50)
            mock_req.assert_called_once_with(
                "GET", "/v1/goals", params={"page": 1, "per_page": 50}
            )

    async def test_get_reviews_calls_correct_path(self) -> None:
        client = self._client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"data": [SAMPLE_REVIEW], "meta": {"total_pages": 1}}
            await client.get_reviews(page=1, per_page=50)
            mock_req.assert_called_once_with(
                "GET", "/v1/reviews", params={"page": 1, "per_page": 50}
            )

    async def test_get_feedback_calls_correct_path(self) -> None:
        client = self._client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"data": [SAMPLE_FEEDBACK], "meta": {"total_pages": 1}}
            await client.get_feedback(page=1, per_page=50)
            mock_req.assert_called_once_with(
                "GET", "/v1/feedback", params={"page": 1, "per_page": 50}
            )

    async def test_get_one_on_ones_calls_correct_path(self) -> None:
        client = self._client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = {"data": [], "meta": {"total_pages": 1}}
            await client.get_one_on_ones(page=1, per_page=50)
            mock_req.assert_called_once_with(
                "GET", "/v1/one-on-ones", params={"page": 1, "per_page": 50}
            )

    async def test_raise_for_status_401_raises_auth_error(self) -> None:
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.status = 401
        mock_resp.headers = {}
        mock_resp.json = AsyncMock(return_value={"error": "Unauthorized"})
        with pytest.raises(LatticeAuthError):
            await client._raise_for_status(mock_resp)

    async def test_raise_for_status_403_raises_auth_error(self) -> None:
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.status = 403
        mock_resp.headers = {}
        mock_resp.json = AsyncMock(return_value={"error": "Forbidden"})
        with pytest.raises(LatticeAuthError):
            await client._raise_for_status(mock_resp)

    async def test_raise_for_status_404_raises_not_found(self) -> None:
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.headers = {}
        mock_resp.json = AsyncMock(return_value={"error": "Not Found"})
        with pytest.raises(LatticeNotFoundError):
            await client._raise_for_status(mock_resp)

    async def test_raise_for_status_429_raises_rate_limit(self) -> None:
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.status = 429
        mock_resp.headers = {"Retry-After": "60"}
        mock_resp.json = AsyncMock(return_value={"error": "Too Many Requests"})
        with pytest.raises(LatticeRateLimitError) as exc_info:
            await client._raise_for_status(mock_resp)
        assert exc_info.value.retry_after == 60.0

    async def test_raise_for_status_500_raises_network_error(self) -> None:
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.headers = {}
        mock_resp.json = AsyncMock(return_value={"error": "Internal Server Error"})
        with pytest.raises(LatticeNetworkError):
            await client._raise_for_status(mock_resp)

    async def test_raise_for_status_200_returns_json(self) -> None:
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"data": [SAMPLE_USER]})
        result = await client._raise_for_status(mock_resp)
        assert result == {"data": [SAMPLE_USER]}


# ══════════════════════════════════════════════════════════════════════════════
# 6 — install()
# ══════════════════════════════════════════════════════════════════════════════


class TestInstall:
    async def test_install_success(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(return_value=ONE_PAGE_RESPONSE)
            mock_make.return_value = mock_client
            result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Connected" in result.message

    async def test_install_missing_api_token(self) -> None:
        conn = LatticeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_token" in result.message

    async def test_install_invalid_token(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(side_effect=LatticeAuthError("401"))
            mock_make.return_value = mock_client
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(side_effect=LatticeNetworkError("timeout"))
            mock_make.return_value = mock_client
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_install_sets_connector_id(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(return_value=ONE_PAGE_RESPONSE)
            mock_make.return_value = mock_client
            result = await conn.install()
        assert result.connector_id == CONNECTOR_ID


# ══════════════════════════════════════════════════════════════════════════════
# 7 — health_check()
# ══════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    async def test_health_check_healthy(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(return_value=ONE_PAGE_RESPONSE)
            mock_make.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "reachable" in result.message

    async def test_health_check_auth_error(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(side_effect=LatticeAuthError("401"))
            mock_make.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(side_effect=LatticeNetworkError("timeout"))
            mock_make.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_missing_token(self) -> None:
        conn = LatticeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_unexpected_error(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(side_effect=RuntimeError("unexpected"))
            mock_make.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ══════════════════════════════════════════════════════════════════════════════
# 8 — sync()
# ══════════════════════════════════════════════════════════════════════════════


class TestSync:
    def _patched_conn(
        self,
        users_resp: list,
        goals_resp: list,
        reviews_resp: list,
    ) -> LatticeConnector:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            return_value={"data": users_resp, "meta": {"total_pages": 1}}
        )
        mock_client.get_goals = AsyncMock(
            return_value={"data": goals_resp, "meta": {"total_pages": 1}}
        )
        mock_client.get_reviews = AsyncMock(
            return_value={"data": reviews_resp, "meta": {"total_pages": 1}}
        )
        mock_client.get_feedback = AsyncMock(
            return_value={"data": [], "meta": {"total_pages": 1}}
        )
        mock_client.get_departments = AsyncMock(
            return_value={"data": [], "meta": {"total_pages": 1}}
        )
        conn._http_client = mock_client
        return conn

    async def test_sync_returns_sync_result(self) -> None:
        conn = self._patched_conn([SAMPLE_USER], [SAMPLE_GOAL], [SAMPLE_REVIEW])
        result = await conn.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_counts_users_and_goals_and_reviews(self) -> None:
        conn = self._patched_conn([SAMPLE_USER], [SAMPLE_GOAL], [SAMPLE_REVIEW])
        result = await conn.sync()
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.documents_failed == 0

    async def test_sync_completed_status(self) -> None:
        conn = self._patched_conn([SAMPLE_USER], [SAMPLE_GOAL], [SAMPLE_REVIEW])
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_empty_resources(self) -> None:
        conn = self._patched_conn([], [], [])
        result = await conn.sync()
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_users_fatal_on_error(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(side_effect=LatticeError("fatal"))
        conn._http_client = mock_client
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_goals_nonfatal(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            return_value={"data": [SAMPLE_USER], "meta": {"total_pages": 1}}
        )
        mock_client.get_goals = AsyncMock(side_effect=LatticeNetworkError("goals down"))
        mock_client.get_reviews = AsyncMock(
            return_value={"data": [], "meta": {"total_pages": 1}}
        )
        conn._http_client = mock_client
        result = await conn.sync()
        # Users still synced
        assert result.documents_synced >= 1

    async def test_sync_reviews_nonfatal(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            return_value={"data": [SAMPLE_USER], "meta": {"total_pages": 1}}
        )
        mock_client.get_goals = AsyncMock(
            return_value={"data": [], "meta": {"total_pages": 1}}
        )
        mock_client.get_reviews = AsyncMock(side_effect=LatticeNetworkError("reviews down"))
        conn._http_client = mock_client
        result = await conn.sync()
        assert result.documents_synced >= 1

    async def test_sync_multiple_users(self) -> None:
        conn = self._patched_conn([SAMPLE_USER, SAMPLE_USER_2], [], [])
        result = await conn.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_partial_status_on_failed_normalize(self) -> None:
        conn = _make_connector()
        # Inject a user that causes normalize_user to fail due to bad data
        bad_user: dict = {}  # missing id — normalize will still succeed, but let's force
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            return_value={"data": [SAMPLE_USER], "meta": {"total_pages": 1}}
        )
        mock_client.get_goals = AsyncMock(
            return_value={"data": [], "meta": {"total_pages": 1}}
        )
        mock_client.get_reviews = AsyncMock(
            return_value={"data": [], "meta": {"total_pages": 1}}
        )
        conn._http_client = mock_client
        # Patch normalize_user to raise for testing partial status
        with patch("connector.normalize_user", side_effect=ValueError("bad")):
            result = await conn.sync()
        assert result.documents_failed == 1
        assert result.status == SyncStatus.PARTIAL


# ══════════════════════════════════════════════════════════════════════════════
# 9 — list_users / list_goals / list_reviews / list_feedback / list_departments
# ══════════════════════════════════════════════════════════════════════════════


class TestListMethods:
    async def test_list_users_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value=ONE_PAGE_RESPONSE)
        conn._http_client = mock_client
        users = await conn.list_users()
        assert isinstance(users, list)
        assert len(users) == 1

    async def test_list_users_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value=EMPTY_RESPONSE)
        conn._http_client = mock_client
        users = await conn.list_users()
        assert users == []

    async def test_list_users_pagination(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            side_effect=[TWO_PAGE_RESPONSE_P1, TWO_PAGE_RESPONSE_P2]
        )
        conn._http_client = mock_client
        users = await conn.list_users()
        assert len(users) == 2

    async def test_list_goals_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_goals = AsyncMock(
            return_value={"data": [SAMPLE_GOAL], "meta": {"total_pages": 1}}
        )
        conn._http_client = mock_client
        goals = await conn.list_goals()
        assert isinstance(goals, list)
        assert len(goals) == 1

    async def test_list_goals_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_goals = AsyncMock(return_value=EMPTY_RESPONSE)
        conn._http_client = mock_client
        goals = await conn.list_goals()
        assert goals == []

    async def test_list_reviews_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_reviews = AsyncMock(
            return_value={"data": [SAMPLE_REVIEW], "meta": {"total_pages": 1}}
        )
        conn._http_client = mock_client
        reviews = await conn.list_reviews()
        assert isinstance(reviews, list)
        assert len(reviews) == 1

    async def test_list_reviews_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_reviews = AsyncMock(return_value=EMPTY_RESPONSE)
        conn._http_client = mock_client
        reviews = await conn.list_reviews()
        assert reviews == []

    async def test_list_feedback_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_feedback = AsyncMock(
            return_value={"data": [SAMPLE_FEEDBACK], "meta": {"total_pages": 1}}
        )
        conn._http_client = mock_client
        feedback = await conn.list_feedback()
        assert isinstance(feedback, list)
        assert len(feedback) == 1

    async def test_list_feedback_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_feedback = AsyncMock(return_value=EMPTY_RESPONSE)
        conn._http_client = mock_client
        feedback = await conn.list_feedback()
        assert feedback == []

    async def test_list_departments_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_departments = AsyncMock(
            return_value={"data": [SAMPLE_DEPARTMENT], "meta": {"total_pages": 1}}
        )
        conn._http_client = mock_client
        departments = await conn.list_departments()
        assert isinstance(departments, list)
        assert len(departments) == 1

    async def test_list_departments_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_departments = AsyncMock(return_value=EMPTY_RESPONSE)
        conn._http_client = mock_client
        departments = await conn.list_departments()
        assert departments == []

    async def test_list_reviews_pagination(self) -> None:
        conn = _make_connector()
        page1 = {"data": [SAMPLE_REVIEW], "meta": {"total_pages": 2}}
        review2 = dict(SAMPLE_REVIEW)
        review2["id"] = "r002"
        page2 = {"data": [review2], "meta": {"total_pages": 2}}
        mock_client = MagicMock()
        mock_client.get_reviews = AsyncMock(side_effect=[page1, page2])
        conn._http_client = mock_client
        reviews = await conn.list_reviews()
        assert len(reviews) == 2


# ══════════════════════════════════════════════════════════════════════════════
# 10 — get_user()
# ══════════════════════════════════════════════════════════════════════════════


class TestGetUser:
    async def test_get_user_returns_document(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_user = AsyncMock(return_value={"data": SAMPLE_USER})
        conn._http_client = mock_client
        doc = await conn.get_user("u001")
        assert isinstance(doc, ConnectorDocument)
        assert "Jane Smith" in doc.title

    async def test_get_user_not_found_raises(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_user = AsyncMock(side_effect=LatticeNotFoundError("user", "u999"))
        conn._http_client = mock_client
        with pytest.raises(LatticeNotFoundError):
            await conn.get_user("u999")

    async def test_get_user_passes_id(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_user = AsyncMock(return_value={"data": SAMPLE_USER})
        conn._http_client = mock_client
        await conn.get_user("u001")
        mock_client.get_user.assert_called_once_with("u001")

    async def test_get_user_integer_id(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_user = AsyncMock(return_value={"data": SAMPLE_USER})
        conn._http_client = mock_client
        doc = await conn.get_user(1)
        assert isinstance(doc, ConnectorDocument)


# ══════════════════════════════════════════════════════════════════════════════
# 11 — Connector config, lifecycle, constants
# ══════════════════════════════════════════════════════════════════════════════


class TestConnectorConfig:
    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "lattice"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_type_class_attr(self) -> None:
        assert LatticeConnector.CONNECTOR_TYPE == "lattice"

    def test_auth_type_class_attr(self) -> None:
        assert LatticeConnector.AUTH_TYPE == "api_key"

    def test_api_token_from_kwarg(self) -> None:
        conn = LatticeConnector(api_token="tok123")
        assert conn._api_token == "tok123"

    def test_api_token_from_config(self) -> None:
        conn = LatticeConnector(config={"api_token": "cfg_tok"})
        assert conn._api_token == "cfg_tok"

    def test_config_takes_precedence_over_kwarg(self) -> None:
        conn = LatticeConnector(
            config={"api_token": "cfg_tok"}, api_token="kwarg_tok"
        )
        assert conn._api_token == "cfg_tok"

    def test_missing_credentials_all(self) -> None:
        conn = LatticeConnector()
        assert "api_token" in conn._missing_credentials()

    def test_missing_credentials_none_when_set(self) -> None:
        conn = _make_connector()
        assert conn._missing_credentials() == []

    def test_ensure_client_creates_client(self) -> None:
        conn = _make_connector()
        assert conn._http_client is None
        client = conn._ensure_client()
        assert client is not None
        assert conn._http_client is client

    def test_ensure_client_reuses_client(self) -> None:
        conn = _make_connector()
        c1 = conn._ensure_client()
        c2 = conn._ensure_client()
        assert c1 is c2

    async def test_aclose_clears_client(self) -> None:
        conn = _make_connector()
        conn._ensure_client()
        assert conn._http_client is not None
        await conn.aclose()
        assert conn._http_client is None

    async def test_context_manager(self) -> None:
        async with LatticeConnector(api_token=API_TOKEN) as conn:
            assert isinstance(conn, LatticeConnector)
        assert conn._http_client is None

    def test_tenant_id_stored(self) -> None:
        conn = LatticeConnector(tenant_id=TENANT_ID, api_token=API_TOKEN)
        assert conn.tenant_id == TENANT_ID

    def test_connector_id_stored(self) -> None:
        conn = LatticeConnector(connector_id=CONNECTOR_ID, api_token=API_TOKEN)
        assert conn.connector_id == CONNECTOR_ID


# ══════════════════════════════════════════════════════════════════════════════
# 12 — Pagination (meta.total via math.ceil)
# ══════════════════════════════════════════════════════════════════════════════


class TestPagination:
    async def test_pagination_via_total_meta(self) -> None:
        """Verify _fetch_all_pages can derive total_pages from meta.total."""
        conn = _make_connector()
        # 3 items, per_page=2 → ceil(3/2)=2 pages
        page1 = {"data": [SAMPLE_USER, SAMPLE_USER_2], "meta": {"total": 3}}
        user3 = dict(SAMPLE_USER)
        user3["id"] = "u003"
        page2 = {"data": [user3], "meta": {"total": 3}}
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(side_effect=[page1, page2])
        conn._http_client = mock_client

        items = await conn._fetch_all_pages(mock_client.get_users, "data", per_page=2)
        assert len(items) == 3

    async def test_pagination_stops_on_empty_data(self) -> None:
        """Verify _fetch_all_pages stops when data array is empty."""
        conn = _make_connector()
        page1 = {"data": [SAMPLE_USER], "meta": {}}
        page2 = {"data": [], "meta": {}}
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(side_effect=[page1, page2])
        conn._http_client = mock_client
        items = await conn._fetch_all_pages(mock_client.get_users, "data")
        assert len(items) == 1
