"""Unit tests for FifteenFiveConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import FifteenFiveConnector
from exceptions import (
    FifteenFiveAuthError,
    FifteenFiveError,
    FifteenFiveNetworkError,
    FifteenFiveNotFoundError,
    FifteenFiveRateLimitError,
)
from helpers.utils import (
    normalize_high_five,
    normalize_objective,
    normalize_report,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ──────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_fifteen_five_test_001"
API_KEY = "FIFTEEN_FIVE_TEST_API_KEY"

SAMPLE_USER: dict = {
    "id": 1001,
    "email": "alice@acme.com",
    "first_name": "Alice",
    "last_name": "Walker",
    "is_active": True,
}

SAMPLE_USER_2: dict = {
    "id": 1002,
    "email": "bob@acme.com",
    "first_name": "Bob",
    "last_name": "Smith",
    "is_active": True,
}

SAMPLE_USERS_PAGE_1: dict = {
    "count": 2,
    "next": "https://my.15five.com/api/public/v1/user/?page=2",
    "previous": None,
    "results": [SAMPLE_USER],
}

SAMPLE_USERS_PAGE_2: dict = {
    "count": 2,
    "next": None,
    "previous": "https://my.15five.com/api/public/v1/user/?page=1",
    "results": [SAMPLE_USER_2],
}

SAMPLE_USERS_SINGLE_PAGE: dict = {
    "count": 1,
    "next": None,
    "previous": None,
    "results": [SAMPLE_USER],
}

SAMPLE_REPORT: dict = {
    "id": 5001,
    "responder": {"id": 1001, "name": "Alice Walker"},
    "created_at": "2026-06-01T09:00:00Z",
    "is_complete": True,
    "high_fives_count": 2,
}

SAMPLE_REPORT_2: dict = {
    "id": 5002,
    "responder": {"id": 1002, "name": "Bob Smith"},
    "created_at": "2026-06-08T09:00:00Z",
    "is_complete": False,
    "high_fives_count": 0,
}

SAMPLE_REPORTS_PAGE: dict = {
    "count": 2,
    "next": None,
    "previous": None,
    "results": [SAMPLE_REPORT, SAMPLE_REPORT_2],
}

SAMPLE_OBJECTIVE: dict = {
    "id": 2001,
    "name": "Increase NPS to 70",
    "description": "Improve customer satisfaction scores",
    "owner": {"id": 1001, "name": "Alice Walker"},
    "progress": 65.0,
    "status": "on_track",
    "start_date": "2026-01-01",
    "due_date": "2026-06-30",
}

SAMPLE_OBJECTIVES_PAGE: dict = {
    "count": 1,
    "next": None,
    "previous": None,
    "results": [SAMPLE_OBJECTIVE],
}

SAMPLE_HIGH_FIVE: dict = {
    "id": 3001,
    "sender": {"id": 1001, "name": "Alice Walker"},
    "receivers": [{"id": 1002, "name": "Bob Smith"}],
    "message": "Great work on the Q2 launch!",
    "created_at": "2026-06-15T14:00:00Z",
}

SAMPLE_HIGH_FIVE_2: dict = {
    "id": 3002,
    "sender": {"id": 1002, "name": "Bob Smith"},
    "receivers": [
        {"id": 1001, "name": "Alice Walker"},
        {"id": 1003, "name": "Carol Jones"},
    ],
    "message": "Awesome collaboration!",
    "created_at": "2026-06-16T10:00:00Z",
}

SAMPLE_HIGH_FIVES_PAGE: dict = {
    "count": 2,
    "next": None,
    "previous": None,
    "results": [SAMPLE_HIGH_FIVE, SAMPLE_HIGH_FIVE_2],
}

SAMPLE_MEETING: dict = {
    "id": 4001,
    "title": "1-on-1: Alice & Bob",
    "date": "2026-06-10",
    "participants": [{"id": 1001}, {"id": 1002}],
}

SAMPLE_MEETINGS_PAGE: dict = {
    "count": 1,
    "next": None,
    "previous": None,
    "results": [SAMPLE_MEETING],
}

SAMPLE_GROUP: dict = {
    "id": 6001,
    "name": "Engineering",
    "members_count": 12,
}

SAMPLE_GROUPS_PAGE: dict = {
    "count": 1,
    "next": None,
    "previous": None,
    "results": [SAMPLE_GROUP],
}

EMPTY_PAGE: dict = {
    "count": 0,
    "next": None,
    "previous": None,
    "results": [],
}


def make_connector(api_key: str = API_KEY) -> FifteenFiveConnector:
    return FifteenFiveConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key},
    )


# ── Exception hierarchy tests ─────────────────────────────────────────────────

class TestExceptions:
    def test_base_error_attributes(self) -> None:
        err = FifteenFiveError("something broke", status_code=400, code="bad_request")
        assert str(err) == "something broke"
        assert err.status_code == 400
        assert err.code == "bad_request"

    def test_base_error_defaults(self) -> None:
        err = FifteenFiveError("plain error")
        assert err.status_code == 0
        assert err.code == ""

    def test_auth_error_is_base(self) -> None:
        err = FifteenFiveAuthError("auth failed", status_code=401, code="auth_error")
        assert isinstance(err, FifteenFiveError)
        assert err.status_code == 401

    def test_rate_limit_error_attributes(self) -> None:
        err = FifteenFiveRateLimitError("too many requests", retry_after=30.0)
        assert err.status_code == 429
        assert err.code == "rate_limit"
        assert err.retry_after == 30.0

    def test_rate_limit_error_default_retry_after(self) -> None:
        err = FifteenFiveRateLimitError("rate limited")
        assert err.retry_after == 0.0

    def test_not_found_error_format(self) -> None:
        err = FifteenFiveNotFoundError("report", 5001)
        assert "5001" in str(err)
        assert err.status_code == 404
        assert err.code == "resource_missing"

    def test_network_error_is_base(self) -> None:
        err = FifteenFiveNetworkError("timeout", status_code=503)
        assert isinstance(err, FifteenFiveError)
        assert err.status_code == 503

    def test_all_errors_inherit_from_base(self) -> None:
        for cls in [
            FifteenFiveAuthError,
            FifteenFiveNetworkError,
        ]:
            err = cls("test")
            assert isinstance(err, FifteenFiveError)

        assert isinstance(FifteenFiveRateLimitError("test"), FifteenFiveError)
        assert isinstance(FifteenFiveNotFoundError("x", 1), FifteenFiveError)


# ── Models tests ──────────────────────────────────────────────────────────────

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
        assert SyncStatus.RUNNING == "running"

    def test_install_result_defaults(self) -> None:
        from models import InstallResult
        r = InstallResult(
            health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED
        )
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result(self) -> None:
        from models import HealthCheckResult
        r = HealthCheckResult(
            health=ConnectorHealth.DEGRADED,
            auth_status=AuthStatus.INVALID_CREDENTIALS,
            message="401 Unauthorized",
        )
        assert r.health == ConnectorHealth.DEGRADED
        assert r.message == "401 Unauthorized"

    def test_sync_result_defaults(self) -> None:
        from models import SyncResult
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_connector_document_metadata_default(self) -> None:
        from models import ConnectorDocument
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="body",
            connector_id="c1",
            tenant_id="t1",
        )
        assert doc.metadata == {}
        assert doc.source_url == ""


# ── Normalizer tests ──────────────────────────────────────────────────────────

class TestNormalizeReport:
    def test_stable_source_id(self) -> None:
        doc1 = normalize_report(SAMPLE_REPORT)
        doc2 = normalize_report(SAMPLE_REPORT)
        assert doc1.source_id == doc2.source_id

    def test_source_id_format(self) -> None:
        doc = normalize_report(SAMPLE_REPORT)
        expected = hashlib.sha256(b"report:5001").hexdigest()[:16]
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_different_reports_different_ids(self) -> None:
        doc1 = normalize_report(SAMPLE_REPORT)
        doc2 = normalize_report(SAMPLE_REPORT_2)
        assert doc1.source_id != doc2.source_id

    def test_type_in_metadata(self) -> None:
        doc = normalize_report(SAMPLE_REPORT)
        assert doc.metadata["type"] == "checkin"

    def test_responder_object_extracted(self) -> None:
        doc = normalize_report(SAMPLE_REPORT)
        assert "Alice Walker" in doc.title or "Alice Walker" in doc.content

    def test_responder_string_fallback(self) -> None:
        r = {**SAMPLE_REPORT, "responder": "Dave"}
        doc = normalize_report(r)
        assert "Dave" in doc.title or "Dave" in doc.content

    def test_source_url_contains_id(self) -> None:
        doc = normalize_report(SAMPLE_REPORT)
        assert "5001" in doc.source_url
        assert "15five.com" in doc.source_url

    def test_missing_report_id(self) -> None:
        doc = normalize_report({})
        assert doc.source_id == hashlib.sha256(b"report:").hexdigest()[:16]

    def test_is_complete_in_content(self) -> None:
        doc = normalize_report(SAMPLE_REPORT)
        assert "True" in doc.content or "Complete" in doc.content


class TestNormalizeObjective:
    def test_stable_source_id(self) -> None:
        doc1 = normalize_objective(SAMPLE_OBJECTIVE)
        doc2 = normalize_objective(SAMPLE_OBJECTIVE)
        assert doc1.source_id == doc2.source_id

    def test_source_id_format(self) -> None:
        doc = normalize_objective(SAMPLE_OBJECTIVE)
        expected = hashlib.sha256(b"objective:2001").hexdigest()[:16]
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_type_in_metadata(self) -> None:
        doc = normalize_objective(SAMPLE_OBJECTIVE)
        assert doc.metadata["type"] == "objective"

    def test_name_in_title(self) -> None:
        doc = normalize_objective(SAMPLE_OBJECTIVE)
        assert "Increase NPS to 70" in doc.title

    def test_owner_object_extracted(self) -> None:
        doc = normalize_objective(SAMPLE_OBJECTIVE)
        assert "Alice Walker" in doc.content

    def test_owner_string_fallback(self) -> None:
        o = {**SAMPLE_OBJECTIVE, "owner": "Dave"}
        doc = normalize_objective(o)
        assert "Dave" in doc.content

    def test_progress_in_content(self) -> None:
        doc = normalize_objective(SAMPLE_OBJECTIVE)
        assert "65" in doc.content

    def test_source_url_contains_id(self) -> None:
        doc = normalize_objective(SAMPLE_OBJECTIVE)
        assert "2001" in doc.source_url

    def test_different_objectives_different_ids(self) -> None:
        o2 = {**SAMPLE_OBJECTIVE, "id": 9999}
        doc1 = normalize_objective(SAMPLE_OBJECTIVE)
        doc2 = normalize_objective(o2)
        assert doc1.source_id != doc2.source_id


class TestNormalizeHighFive:
    def test_stable_source_id(self) -> None:
        doc1 = normalize_high_five(SAMPLE_HIGH_FIVE)
        doc2 = normalize_high_five(SAMPLE_HIGH_FIVE)
        assert doc1.source_id == doc2.source_id

    def test_source_id_format(self) -> None:
        doc = normalize_high_five(SAMPLE_HIGH_FIVE)
        expected = hashlib.sha256(b"highfive:3001").hexdigest()[:16]
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_type_in_metadata(self) -> None:
        doc = normalize_high_five(SAMPLE_HIGH_FIVE)
        assert doc.metadata["type"] == "recognition"

    def test_sender_object_extracted(self) -> None:
        doc = normalize_high_five(SAMPLE_HIGH_FIVE)
        assert "Alice Walker" in doc.title or "Alice Walker" in doc.content

    def test_receiver_list_extracted(self) -> None:
        doc = normalize_high_five(SAMPLE_HIGH_FIVE)
        assert "Bob Smith" in doc.content or "Bob Smith" in doc.title

    def test_multiple_receivers(self) -> None:
        doc = normalize_high_five(SAMPLE_HIGH_FIVE_2)
        assert "Alice Walker" in doc.content or "Carol Jones" in doc.content

    def test_message_in_content(self) -> None:
        doc = normalize_high_five(SAMPLE_HIGH_FIVE)
        assert "Great work on the Q2 launch!" in doc.content

    def test_source_url_contains_id(self) -> None:
        doc = normalize_high_five(SAMPLE_HIGH_FIVE)
        assert "3001" in doc.source_url

    def test_different_highfives_different_ids(self) -> None:
        doc1 = normalize_high_five(SAMPLE_HIGH_FIVE)
        doc2 = normalize_high_five(SAMPLE_HIGH_FIVE_2)
        assert doc1.source_id != doc2.source_id

    def test_sender_string_fallback(self) -> None:
        h = {**SAMPLE_HIGH_FIVE, "sender": "Dave"}
        doc = normalize_high_five(h)
        assert "Dave" in doc.title or "Dave" in doc.content


# ── with_retry tests ──────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                FifteenFiveNetworkError("timeout"),
                FifteenFiveNetworkError("timeout"),
                {"ok": True},
            ]
        )
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 3

    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=FifteenFiveAuthError("401"))
        with pytest.raises(FifteenFiveAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    async def test_exhausts_retries_raises_last_exc(self) -> None:
        fn = AsyncMock(side_effect=FifteenFiveNetworkError("always fails"))
        with pytest.raises(FifteenFiveNetworkError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    async def test_rate_limit_retry_with_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[
                FifteenFiveRateLimitError("rate limited", retry_after=0.001),
                {"ok": True},
            ]
        )
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_rate_limit_exhausted_raises(self) -> None:
        fn = AsyncMock(
            side_effect=FifteenFiveRateLimitError("always rate limited")
        )
        with pytest.raises(FifteenFiveRateLimitError):
            await with_retry(fn, max_attempts=2, base_delay=0)
        assert fn.call_count == 2


# ── HTTP client tests ─────────────────────────────────────────────────────────

class TestFifteenFiveHTTPClient:
    """Tests for FifteenFiveHTTPClient with mocked aiohttp sessions."""

    def _make_mock_response(
        self,
        status: int,
        json_data: dict | None = None,
        headers: dict | None = None,
    ) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.headers = headers or {}
        resp.json = AsyncMock(return_value=json_data or {})
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    def _make_mock_session(self, response: MagicMock) -> MagicMock:
        session = MagicMock()
        session.request = MagicMock(return_value=response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    async def test_get_users_bearer_header(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(200, SAMPLE_USERS_SINGLE_PAGE)
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_users(API_KEY)

        call_kwargs = session.request.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("Authorization") == f"Bearer {API_KEY}"
        assert result == SAMPLE_USERS_SINGLE_PAGE

    async def test_get_users_trailing_slash_url(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(200, SAMPLE_USERS_SINGLE_PAGE)
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            await client.get_users(API_KEY)

        call_args = session.request.call_args
        url = call_args.args[1] if len(call_args.args) > 1 else call_args[0][1]
        assert url.endswith("/")

    async def test_get_reports_returns_drf_page(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(200, SAMPLE_REPORTS_PAGE)
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_reports(API_KEY)

        assert "results" in result
        assert result["count"] == 2

    async def test_get_report_single(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(200, SAMPLE_REPORT)
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_report(API_KEY, 5001)

        assert result["id"] == 5001

    async def test_get_objectives(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(200, SAMPLE_OBJECTIVES_PAGE)
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_objectives(API_KEY)

        assert result["results"][0]["id"] == 2001

    async def test_get_meetings(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(200, SAMPLE_MEETINGS_PAGE)
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_meetings(API_KEY)

        assert result["count"] == 1

    async def test_get_high_fives(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(200, SAMPLE_HIGH_FIVES_PAGE)
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_high_fives(API_KEY)

        assert len(result["results"]) == 2

    async def test_get_groups(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(200, SAMPLE_GROUPS_PAGE)
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_groups(API_KEY)

        assert result["results"][0]["name"] == "Engineering"

    async def test_raise_for_status_401(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(401, {"detail": "Invalid token"})
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(FifteenFiveAuthError) as exc_info:
                await client.get_users(API_KEY)
        assert exc_info.value.status_code == 401

    async def test_raise_for_status_403(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(403, {"detail": "Permission denied"})
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(FifteenFiveAuthError) as exc_info:
                await client.get_users(API_KEY)
        assert exc_info.value.status_code == 403

    async def test_raise_for_status_404(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(404, {"detail": "Not found"})
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(FifteenFiveNotFoundError):
                await client.get_report(API_KEY, 9999)

    async def test_raise_for_status_429(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(
            429, {"detail": "Too many"}, headers={"Retry-After": "60"}
        )
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(FifteenFiveRateLimitError) as exc_info:
                await client.get_users(API_KEY)
        assert exc_info.value.retry_after == 60.0

    async def test_raise_for_status_500(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(500, {"detail": "Server error"})
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(FifteenFiveNetworkError):
                await client.get_users(API_KEY)

    async def test_drf_pagination_next_field(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(200, SAMPLE_USERS_PAGE_1)
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_users(API_KEY, page=1)

        assert result["next"] is not None
        assert result["results"][0]["id"] == 1001

    async def test_accept_header_set(self) -> None:
        from client.http_client import FifteenFiveHTTPClient
        client = FifteenFiveHTTPClient()
        resp = self._make_mock_response(200, SAMPLE_USERS_SINGLE_PAGE)
        session = self._make_mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            await client.get_users(API_KEY)

        call_kwargs = session.request.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("Accept") == "application/json"


# ── install() tests ───────────────────────────────────────────────────────────

class TestInstall:
    async def test_install_success(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value=SAMPLE_USERS_SINGLE_PAGE)
        conn._make_client = MagicMock(return_value=mock_client)

        result = await conn.install()

        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "15Five" in result.message

    async def test_install_missing_api_key(self) -> None:
        conn = make_connector(api_key="")
        conn._api_key = ""
        result = await conn.install()

        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_auth_error(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            side_effect=FifteenFiveAuthError("Invalid token", status_code=401)
        )
        conn._make_client = MagicMock(return_value=mock_client)

        result = await conn.install()

        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            side_effect=FifteenFiveNetworkError("connection refused")
        )
        conn._make_client = MagicMock(return_value=mock_client)

        result = await conn.install()

        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_install_sets_connector_id(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value=SAMPLE_USERS_SINGLE_PAGE)
        conn._make_client = MagicMock(return_value=mock_client)

        result = await conn.install()

        assert result.connector_id == CONNECTOR_ID


# ── health_check() tests ──────────────────────────────────────────────────────

class TestHealthCheck:
    async def test_health_check_success(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value=SAMPLE_USERS_SINGLE_PAGE)
        conn._make_client = MagicMock(return_value=mock_client)

        result = await conn.health_check()

        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "15Five" in result.message

    async def test_health_check_includes_user_count(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value={"count": 42, "next": None, "results": []})
        conn._make_client = MagicMock(return_value=mock_client)

        result = await conn.health_check()

        assert "42" in result.message

    async def test_health_check_missing_key(self) -> None:
        conn = make_connector(api_key="")
        conn._api_key = ""

        result = await conn.health_check()

        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            side_effect=FifteenFiveAuthError("401")
        )
        conn._make_client = MagicMock(return_value=mock_client)

        result = await conn.health_check()

        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            side_effect=FifteenFiveNetworkError("timeout")
        )
        conn._make_client = MagicMock(return_value=mock_client)

        result = await conn.health_check()

        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ── sync() tests ──────────────────────────────────────────────────────────────

class TestSync:
    async def test_sync_completed_counts(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_reports = AsyncMock(return_value=SAMPLE_REPORTS_PAGE)
        mock_client.get_objectives = AsyncMock(return_value=SAMPLE_OBJECTIVES_PAGE)
        mock_client.get_high_fives = AsyncMock(return_value=SAMPLE_HIGH_FIVES_PAGE)
        conn._http_client = mock_client

        result = await conn.sync()

        # reports=2, objectives=1, high_fives=2 = 5 total
        assert result.documents_found == 5
        assert result.documents_synced == 5
        assert result.documents_failed == 0
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_status_completed_on_zero_failures(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_reports = AsyncMock(return_value=EMPTY_PAGE)
        mock_client.get_objectives = AsyncMock(return_value=EMPTY_PAGE)
        mock_client.get_high_fives = AsyncMock(return_value=EMPTY_PAGE)
        conn._http_client = mock_client

        result = await conn.sync()

        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    async def test_sync_fails_when_reports_raises(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_reports = AsyncMock(
            side_effect=FifteenFiveNetworkError("server down")
        )
        conn._http_client = mock_client

        result = await conn.sync()

        assert result.status == SyncStatus.FAILED
        assert "server down" in result.message

    async def test_sync_partial_when_objectives_fails(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_reports = AsyncMock(return_value=SAMPLE_REPORTS_PAGE)
        mock_client.get_objectives = AsyncMock(
            side_effect=FifteenFiveNetworkError("timeout")
        )
        mock_client.get_high_fives = AsyncMock(return_value=SAMPLE_HIGH_FIVES_PAGE)
        conn._http_client = mock_client

        result = await conn.sync()

        # objectives failure is non-fatal; reports+high_fives still synced
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_synced == 4  # 2 reports + 2 high fives

    async def test_sync_high_fives_failure_non_fatal(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_reports = AsyncMock(return_value=SAMPLE_REPORTS_PAGE)
        mock_client.get_objectives = AsyncMock(return_value=SAMPLE_OBJECTIVES_PAGE)
        mock_client.get_high_fives = AsyncMock(
            side_effect=FifteenFiveNetworkError("timeout")
        )
        conn._http_client = mock_client

        result = await conn.sync()

        assert result.status == SyncStatus.COMPLETED
        assert result.documents_synced == 3  # 2 reports + 1 objective

    async def test_sync_sets_connector_id_on_documents(self) -> None:
        conn = make_connector()
        ingested_docs = []

        async def mock_ingest(doc: object, kb_id: str) -> None:
            ingested_docs.append(doc)

        conn._ingest_document = mock_ingest  # type: ignore[method-assign]

        mock_client = MagicMock()
        mock_client.get_reports = AsyncMock(return_value=SAMPLE_REPORTS_PAGE)
        mock_client.get_objectives = AsyncMock(return_value=EMPTY_PAGE)
        mock_client.get_high_fives = AsyncMock(return_value=EMPTY_PAGE)
        conn._http_client = mock_client

        await conn.sync(kb_id="kb_test")

        for doc in ingested_docs:
            assert doc.connector_id == CONNECTOR_ID
            assert doc.tenant_id == TENANT_ID

    async def test_sync_ingest_called_with_kb_id(self) -> None:
        conn = make_connector()
        ingest_calls = []

        async def mock_ingest(doc: object, kb_id: str) -> None:
            ingest_calls.append((doc, kb_id))

        conn._ingest_document = mock_ingest  # type: ignore[method-assign]

        mock_client = MagicMock()
        mock_client.get_reports = AsyncMock(return_value=SAMPLE_REPORTS_PAGE)
        mock_client.get_objectives = AsyncMock(return_value=EMPTY_PAGE)
        mock_client.get_high_fives = AsyncMock(return_value=EMPTY_PAGE)
        conn._http_client = mock_client

        await conn.sync(kb_id="my_kb")

        assert all(kb == "my_kb" for _, kb in ingest_calls)


# ── List method tests ─────────────────────────────────────────────────────────

class TestListMethods:
    async def test_list_users_single_page(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value=SAMPLE_USERS_SINGLE_PAGE)
        conn._http_client = mock_client

        users = await conn.list_users()

        assert len(users) == 1
        assert users[0]["id"] == 1001

    async def test_list_users_multi_page(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            side_effect=[SAMPLE_USERS_PAGE_1, SAMPLE_USERS_PAGE_2]
        )
        conn._http_client = mock_client

        users = await conn.list_users()

        assert len(users) == 2
        assert users[0]["id"] == 1001
        assert users[1]["id"] == 1002

    async def test_list_users_empty(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value=EMPTY_PAGE)
        conn._http_client = mock_client

        users = await conn.list_users()

        assert users == []

    async def test_list_reports(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_reports = AsyncMock(return_value=SAMPLE_REPORTS_PAGE)
        conn._http_client = mock_client

        reports = await conn.list_reports()

        assert len(reports) == 2
        assert reports[0]["id"] == 5001

    async def test_list_objectives(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_objectives = AsyncMock(return_value=SAMPLE_OBJECTIVES_PAGE)
        conn._http_client = mock_client

        objectives = await conn.list_objectives()

        assert len(objectives) == 1
        assert objectives[0]["name"] == "Increase NPS to 70"

    async def test_list_meetings(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_meetings = AsyncMock(return_value=SAMPLE_MEETINGS_PAGE)
        conn._http_client = mock_client

        meetings = await conn.list_meetings()

        assert len(meetings) == 1
        assert meetings[0]["id"] == 4001

    async def test_list_high_fives(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_high_fives = AsyncMock(return_value=SAMPLE_HIGH_FIVES_PAGE)
        conn._http_client = mock_client

        hf = await conn.list_high_fives()

        assert len(hf) == 2

    async def test_list_groups(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_groups = AsyncMock(return_value=SAMPLE_GROUPS_PAGE)
        conn._http_client = mock_client

        groups = await conn.list_groups()

        assert len(groups) == 1
        assert groups[0]["name"] == "Engineering"

    async def test_list_reports_empty(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_reports = AsyncMock(return_value=EMPTY_PAGE)
        conn._http_client = mock_client

        reports = await conn.list_reports()

        assert reports == []

    async def test_list_objectives_multi_page(self) -> None:
        page_1 = {
            "count": 2,
            "next": "https://my.15five.com/api/public/v1/objective/?page=2",
            "previous": None,
            "results": [SAMPLE_OBJECTIVE],
        }
        page_2 = {
            "count": 2,
            "next": None,
            "previous": "...",
            "results": [{"id": 2002, "name": "Goal 2", "progress": 10}],
        }
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_objectives = AsyncMock(side_effect=[page_1, page_2])
        conn._http_client = mock_client

        objectives = await conn.list_objectives()

        assert len(objectives) == 2


# ── Lifecycle tests ───────────────────────────────────────────────────────────

class TestLifecycle:
    async def test_context_manager_enter_returns_connector(self) -> None:
        conn = make_connector()
        async with conn as c:
            assert c is conn

    async def test_context_manager_exit_clears_client(self) -> None:
        conn = make_connector()
        conn._http_client = MagicMock()
        async with conn:
            pass
        assert conn._http_client is None

    async def test_aclose_clears_http_client(self) -> None:
        conn = make_connector()
        conn._http_client = MagicMock()
        await conn.aclose()
        assert conn._http_client is None

    def test_connector_type_constant(self) -> None:
        assert FifteenFiveConnector.CONNECTOR_TYPE == "fifteen_five"

    def test_auth_type_constant(self) -> None:
        assert FifteenFiveConnector.AUTH_TYPE == "api_key"

    def test_config_stored_correctly(self) -> None:
        conn = make_connector()
        assert conn._api_key == API_KEY

    def test_api_key_kwarg_fallback(self) -> None:
        conn = FifteenFiveConnector(api_key="direct_key")
        assert conn._api_key == "direct_key"

    def test_config_api_key_takes_precedence(self) -> None:
        conn = FifteenFiveConnector(config={"api_key": "from_config"}, api_key="fallback")
        assert conn._api_key == "from_config"
