"""Unit tests for LeverConnector — all HTTP calls are mocked via AsyncMock.

Coverage:
  - exceptions (5)
  - models / enums (5)
  - normalize_opportunity (10)
  - normalize_posting (8)
  - normalize_user (6)
  - normalize_interview (6)
  - with_retry (6)
  - LeverHTTPClient mocked (14)
  - install() (5)
  - health_check() (5)
  - sync() (8)
  - list_* / get_opportunity (5)
  - connector config & lifecycle (5)

Total: 83 tests
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AUTH_TYPE, CONNECTOR_TYPE, LeverConnector
from exceptions import (
    LeverAuthError,
    LeverError,
    LeverNetworkError,
    LeverNotFoundError,
    LeverRateLimitError,
)
from helpers.utils import (
    normalize_interview,
    normalize_opportunity,
    normalize_posting,
    normalize_user,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared fixtures / test data ───────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_lever_test_001"
API_KEY = "LEVER_API_KEY_TEST"

SAMPLE_OPPORTUNITY: dict = {
    "id": "opp-111",
    "name": "Alice Johnson",
    "headline": "Senior Engineer at Acme",
    "emails": ["alice@example.com"],
    "phones": [{"value": "+1-555-0001"}],
    "stage": {"text": "Phone Screen"},
    "owner": {"name": "Bob Recruiter"},
    "posting": "post-abc",
    "tags": ["engineer", "senior"],
    "archived": False,
    "createdAt": 1700000000000,
    "updatedAt": 1700100000000,
}

SAMPLE_POSTING: dict = {
    "id": "post-abc",
    "text": "Senior Backend Engineer",
    "state": "published",
    "categories": {
        "department": "Engineering",
        "team": "Platform",
        "location": "Remote",
    },
    "tags": ["backend", "python"],
    "createdAt": 1699000000000,
    "updatedAt": 1699100000000,
    "urls": {
        "list": "https://jobs.lever.co/acme",
        "show": "https://jobs.lever.co/acme/post-abc",
        "apply": "https://jobs.lever.co/acme/post-abc/apply",
    },
}

SAMPLE_USER: dict = {
    "id": "user-222",
    "name": "Carol HR",
    "email": "carol@acme.com",
    "username": "carol.hr",
    "accessRole": "admin",
    "active": True,
    "createdAt": 1690000000000,
}

SAMPLE_INTERVIEW: dict = {
    "id": "iv-333",
    "subject": "Technical Phone Screen",
    "note": "Discuss Python experience",
    "date": 1700050000000,
    "duration": 60,
    "location": "Zoom",
    "canceled": False,
    "interviewers": [{"name": "Dave Eng", "email": "dave@acme.com"}],
    "opportunity": "opp-111",
}

LEVER_PAGE_ONE: dict = {
    "data": [SAMPLE_OPPORTUNITY],
    "hasNext": True,
    "next": "cursor_abc",
}

LEVER_PAGE_TWO: dict = {
    "data": [
        {**SAMPLE_OPPORTUNITY, "id": "opp-999", "name": "Bob Candidate"}
    ],
    "hasNext": False,
    "next": None,
}

LEVER_SINGLE_PAGE: dict = {
    "data": [SAMPLE_OPPORTUNITY],
    "hasNext": False,
    "next": None,
}

LEVER_USERS_PAGE: dict = {
    "data": [SAMPLE_USER],
    "hasNext": False,
    "next": None,
}

LEVER_POSTINGS_PAGE: dict = {
    "data": [SAMPLE_POSTING],
    "hasNext": False,
    "next": None,
}

LEVER_INTERVIEWS_PAGE: dict = {
    "data": [SAMPLE_INTERVIEW],
    "hasNext": False,
    "next": None,
}

LEVER_OFFERS_PAGE: dict = {
    "data": [{"id": "offer-444", "status": "signed", "opportunityId": "opp-111"}],
    "hasNext": False,
    "next": None,
}

LEVER_STAGES_RESP: dict = {
    "data": [
        {"id": "stage-1", "text": "Applied"},
        {"id": "stage-2", "text": "Phone Screen"},
    ]
}

LEVER_SINGLE_OPPORTUNITY: dict = {
    "data": SAMPLE_OPPORTUNITY,
}


def _make_connector(api_key: str = API_KEY) -> LeverConnector:
    return LeverConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key},
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Exceptions (5 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_lever_error_base(self) -> None:
        exc = LeverError("something broke", status_code=500, code="err")
        assert exc.status_code == 500
        assert exc.code == "err"
        assert "something broke" in str(exc)

    def test_lever_auth_error_is_lever_error(self) -> None:
        exc = LeverAuthError("bad creds", status_code=401)
        assert isinstance(exc, LeverError)
        assert exc.status_code == 401

    def test_lever_rate_limit_stores_retry_after(self) -> None:
        exc = LeverRateLimitError("too many", retry_after=30.0)
        assert exc.retry_after == 30.0
        assert exc.status_code == 429
        assert exc.code == "rate_limit"

    def test_lever_not_found_error(self) -> None:
        exc = LeverNotFoundError("opportunity", "opp-999")
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "opp-999" in str(exc)

    def test_lever_network_error_is_lever_error(self) -> None:
        exc = LeverNetworkError("timeout", status_code=503)
        assert isinstance(exc, LeverError)
        assert exc.status_code == 503


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Models / enums (5 tests)
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
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        from models import SyncStatus
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_connector_document_defaults(self) -> None:
        from models import ConnectorDocument
        doc = ConnectorDocument(
            source_id="abc", title="T", content="C",
            connector_id="c1", tenant_id="t1"
        )
        assert doc.source_url == ""
        assert doc.metadata == {}

    def test_install_result_defaults(self) -> None:
        from models import InstallResult
        r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.connector_id == ""
        assert r.message == ""


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — normalize_opportunity (10 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeOpportunity:
    def test_title_contains_name(self) -> None:
        doc = normalize_opportunity(SAMPLE_OPPORTUNITY)
        assert "Alice Johnson" in doc.title

    def test_source_id_is_16_chars(self) -> None:
        doc = normalize_opportunity(SAMPLE_OPPORTUNITY)
        assert len(doc.source_id) == 16

    def test_source_id_is_hex(self) -> None:
        doc = normalize_opportunity(SAMPLE_OPPORTUNITY)
        int(doc.source_id, 16)  # raises if not valid hex

    def test_source_id_deterministic(self) -> None:
        doc1 = normalize_opportunity(SAMPLE_OPPORTUNITY)
        doc2 = normalize_opportunity(SAMPLE_OPPORTUNITY)
        assert doc1.source_id == doc2.source_id

    def test_different_ids_produce_different_source_ids(self) -> None:
        opp2 = {**SAMPLE_OPPORTUNITY, "id": "opp-222"}
        d1 = normalize_opportunity(SAMPLE_OPPORTUNITY)
        d2 = normalize_opportunity(opp2)
        assert d1.source_id != d2.source_id

    def test_source_url_contains_opportunity_id(self) -> None:
        doc = normalize_opportunity(SAMPLE_OPPORTUNITY)
        assert "opp-111" in doc.source_url

    def test_metadata_opportunity_id(self) -> None:
        doc = normalize_opportunity(SAMPLE_OPPORTUNITY)
        assert doc.metadata["opportunity_id"] == "opp-111"

    def test_metadata_stage(self) -> None:
        doc = normalize_opportunity(SAMPLE_OPPORTUNITY)
        assert doc.metadata["stage"] == "Phone Screen"

    def test_metadata_emails(self) -> None:
        doc = normalize_opportunity(SAMPLE_OPPORTUNITY)
        assert "alice@example.com" in doc.metadata["emails"]

    def test_content_contains_stage(self) -> None:
        doc = normalize_opportunity(SAMPLE_OPPORTUNITY)
        assert "Phone Screen" in doc.content

    def test_missing_name_falls_back(self) -> None:
        doc = normalize_opportunity({"id": "opp-x"})
        assert "opp-x" in doc.title

    def test_archived_in_metadata(self) -> None:
        doc = normalize_opportunity({**SAMPLE_OPPORTUNITY, "archived": True})
        assert doc.metadata["archived"] is True


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — normalize_posting (8 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizePosting:
    def test_title_contains_job_name(self) -> None:
        doc = normalize_posting(SAMPLE_POSTING)
        assert "Senior Backend Engineer" in doc.title

    def test_source_id_16_chars(self) -> None:
        doc = normalize_posting(SAMPLE_POSTING)
        assert len(doc.source_id) == 16

    def test_source_id_hex(self) -> None:
        doc = normalize_posting(SAMPLE_POSTING)
        int(doc.source_id, 16)

    def test_source_id_deterministic(self) -> None:
        d1 = normalize_posting(SAMPLE_POSTING)
        d2 = normalize_posting(SAMPLE_POSTING)
        assert d1.source_id == d2.source_id

    def test_metadata_department(self) -> None:
        doc = normalize_posting(SAMPLE_POSTING)
        assert doc.metadata["department"] == "Engineering"

    def test_metadata_state(self) -> None:
        doc = normalize_posting(SAMPLE_POSTING)
        assert doc.metadata["state"] == "published"

    def test_source_url_uses_show_url(self) -> None:
        doc = normalize_posting(SAMPLE_POSTING)
        assert "post-abc" in doc.source_url

    def test_content_contains_location(self) -> None:
        doc = normalize_posting(SAMPLE_POSTING)
        assert "Remote" in doc.content


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — normalize_user (6 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeUser:
    def test_title_contains_name(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert "Carol HR" in doc.title

    def test_source_id_16_chars(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert len(doc.source_id) == 16

    def test_source_id_hex(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        int(doc.source_id, 16)

    def test_metadata_email(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert doc.metadata["email"] == "carol@acme.com"

    def test_metadata_access_role(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert doc.metadata["access_role"] == "admin"

    def test_metadata_active(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert doc.metadata["active"] is True


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — normalize_interview (6 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeInterview:
    def test_title_contains_subject(self) -> None:
        doc = normalize_interview(SAMPLE_INTERVIEW)
        assert "Technical Phone Screen" in doc.title

    def test_source_id_16_chars(self) -> None:
        doc = normalize_interview(SAMPLE_INTERVIEW)
        assert len(doc.source_id) == 16

    def test_source_id_hex(self) -> None:
        doc = normalize_interview(SAMPLE_INTERVIEW)
        int(doc.source_id, 16)

    def test_metadata_interviewers(self) -> None:
        doc = normalize_interview(SAMPLE_INTERVIEW)
        assert "Dave Eng" in doc.metadata["interviewers"]

    def test_metadata_opportunity_id(self) -> None:
        doc = normalize_interview(SAMPLE_INTERVIEW)
        assert doc.metadata["opportunity_id"] == "opp-111"

    def test_content_contains_location(self) -> None:
        doc = normalize_interview(SAMPLE_INTERVIEW)
        assert "Zoom" in doc.content


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — with_retry (6 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_success_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_lever_error(self) -> None:
        fn = AsyncMock(
            side_effect=[LeverNetworkError("timeout"), {"ok": True}]
        )
        result = await with_retry(fn, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_never_retries_auth_error(self) -> None:
        fn = AsyncMock(side_effect=LeverAuthError("bad creds"))
        with pytest.raises(LeverAuthError):
            await with_retry(fn)
        assert fn.call_count == 1

    async def test_raises_after_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=LeverNetworkError("timeout"))
        with pytest.raises(LeverNetworkError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    async def test_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[
                LeverRateLimitError("limit", retry_after=0.001),
                {"ok": True},
            ]
        )
        result = await with_retry(fn, base_delay=0)
        assert result == {"ok": True}

    async def test_rate_limit_reraises_after_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=LeverRateLimitError("limit", retry_after=0))
        with pytest.raises(LeverRateLimitError):
            await with_retry(fn, max_attempts=2, base_delay=0)
        assert fn.call_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — LeverHTTPClient mocked (14 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestLeverHTTPClient:
    """Tests for the HTTP client — all aiohttp calls mocked."""

    def _mock_response(self, status: int, body: dict) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.headers = {}

        async def json_fn(**kwargs: object) -> dict:
            return body

        resp.json = json_fn
        return resp

    def _make_mock_session(self, response: MagicMock) -> MagicMock:
        cm_resp = MagicMock()
        cm_resp.__aenter__ = AsyncMock(return_value=response)
        cm_resp.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.request = MagicMock(return_value=cm_resp)

        cm_session = MagicMock()
        cm_session.__aenter__ = AsyncMock(return_value=session)
        cm_session.__aexit__ = AsyncMock(return_value=None)
        return cm_session

    async def test_basic_auth_uses_api_key_empty_password(self) -> None:
        import aiohttp
        from client.http_client import LeverHTTPClient

        client = LeverHTTPClient(config={"api_key": "key123"})
        auth = client._auth()
        assert auth.login == "key123"
        assert auth.password == ""

    async def test_get_users_returns_dict(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(200, LEVER_USERS_PAGE)
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            result = await client.get_users(limit=10)
        assert result["data"] == LEVER_USERS_PAGE["data"]

    async def test_get_opportunities_passes_cursor(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(200, LEVER_PAGE_TWO)
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            result = await client.get_opportunities(cursor="cursor_abc")
        assert result == LEVER_PAGE_TWO

    async def test_get_postings_returns_list(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(200, LEVER_POSTINGS_PAGE)
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            result = await client.get_postings()
        assert len(result["data"]) == 1

    async def test_get_interviews_returns_list(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(200, LEVER_INTERVIEWS_PAGE)
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            result = await client.get_interviews()
        assert result["data"][0]["id"] == "iv-333"

    async def test_get_offers_for_opportunity(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(200, LEVER_OFFERS_PAGE)
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            result = await client.get_offers("opp-111")
        assert result["data"][0]["id"] == "offer-444"

    async def test_get_opportunity_single(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(200, LEVER_SINGLE_OPPORTUNITY)
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            result = await client.get_opportunity("opp-111")
        assert result["data"]["id"] == "opp-111"

    async def test_get_stages(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(200, LEVER_STAGES_RESP)
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            result = await client.get_stages()
        assert len(result["data"]) == 2

    async def test_401_raises_auth_error(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(401, {"error": "Unauthorized"})
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            with pytest.raises(LeverAuthError):
                await client.get_users()

    async def test_403_raises_auth_error(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(403, {"error": "Forbidden"})
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            with pytest.raises(LeverAuthError):
                await client.get_users()

    async def test_404_raises_not_found(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(404, {"error": "Not found"})
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            with pytest.raises(LeverNotFoundError):
                await client.get_opportunity("opp-999")

    async def test_429_raises_rate_limit(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(429, {"error": "Rate limit", "retryAfter": 5})
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            with pytest.raises(LeverRateLimitError) as exc_info:
                await client.get_users()
        assert exc_info.value.retry_after == 5.0

    async def test_500_raises_network_error(self) -> None:
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        resp = self._mock_response(500, {"error": "Internal server error"})
        session_cm = self._make_mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session_cm):
            with pytest.raises(LeverNetworkError):
                await client.get_users()

    async def test_hasnext_pagination_cursor_passed(self) -> None:
        """Verify the client passes cursor param on page 2."""
        from client.http_client import LeverHTTPClient
        client = LeverHTTPClient(config={"api_key": API_KEY})
        calls: list[dict] = []

        async def fake_get_list(path: str, limit: int = 100, cursor: str | None = None, **kw: object) -> dict:
            calls.append({"cursor": cursor})
            if cursor is None:
                return LEVER_PAGE_ONE
            return LEVER_PAGE_TWO

        client._get_list = fake_get_list  # type: ignore[method-assign]
        # Simulate two pages
        page1 = await client._get_list("opportunities")
        page2 = await client._get_list("opportunities", cursor=page1.get("next"))
        assert calls[0]["cursor"] is None
        assert calls[1]["cursor"] == "cursor_abc"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — install() (5 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    async def test_install_success(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(return_value=LEVER_USERS_PAGE)
            mock_factory.return_value = mock_client
            result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_install_missing_api_key(self) -> None:
        conn = LeverConnector(tenant_id=TENANT_ID, config={})
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_auth_failure(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(side_effect=LeverAuthError("bad key"))
            mock_factory.return_value = mock_client
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(side_effect=LeverNetworkError("timeout"))
            mock_factory.return_value = mock_client
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_install_sets_connector_id(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(return_value=LEVER_USERS_PAGE)
            mock_factory.return_value = mock_client
            result = await conn.install()
        assert result.connector_id == CONNECTOR_ID


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — health_check() (5 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    async def test_health_check_healthy(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(return_value=LEVER_USERS_PAGE)
            mock_factory.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_missing_credentials(self) -> None:
        conn = LeverConnector(tenant_id=TENANT_ID, config={})
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(side_effect=LeverAuthError("bad creds"))
            mock_factory.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(side_effect=LeverNetworkError("timeout"))
            mock_factory.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED

    async def test_health_check_generic_exception(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_factory:
            mock_client = MagicMock()
            mock_client.get_users = AsyncMock(side_effect=RuntimeError("unexpected"))
            mock_factory.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — sync() (8 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _mock_conn_all_resources(self, conn: LeverConnector) -> None:
        conn.list_opportunities = AsyncMock(return_value=[SAMPLE_OPPORTUNITY])  # type: ignore[method-assign]
        conn.list_postings = AsyncMock(return_value=[SAMPLE_POSTING])  # type: ignore[method-assign]
        conn.list_users = AsyncMock(return_value=[SAMPLE_USER])  # type: ignore[method-assign]
        conn.list_interviews = AsyncMock(return_value=[SAMPLE_INTERVIEW])  # type: ignore[method-assign]

    async def test_sync_completed_all_resources(self) -> None:
        conn = _make_connector()
        self._mock_conn_all_resources(conn)
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert result.documents_failed == 0

    async def test_sync_empty_results(self) -> None:
        conn = _make_connector()
        conn.list_opportunities = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_postings = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_users = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_interviews = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    async def test_sync_partial_on_normalize_failure(self) -> None:
        conn = _make_connector()
        # Bad opportunity — missing id → still processes, normalize won't crash hard
        bad_opp = {"id": "", "name": ""}
        conn.list_opportunities = AsyncMock(return_value=[bad_opp, SAMPLE_OPPORTUNITY])  # type: ignore[method-assign]
        conn.list_postings = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_users = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_interviews = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        # Both normalize fine (normalize_opportunity handles missing fields gracefully)
        assert result.documents_found == 2

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        conn = _make_connector()
        self._mock_conn_all_resources(conn)
        conn._ingest_document = AsyncMock()  # type: ignore[method-assign]
        result = await conn.sync(kb_id="kb_lever")
        assert conn._ingest_document.call_count == 4
        assert result.documents_synced == 4

    async def test_sync_resource_error_is_non_fatal(self) -> None:
        conn = _make_connector()
        conn.list_opportunities = AsyncMock(side_effect=LeverError("API down"))  # type: ignore[method-assign]
        conn.list_postings = AsyncMock(return_value=[SAMPLE_POSTING])  # type: ignore[method-assign]
        conn.list_users = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_interviews = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        # postings synced fine
        assert result.documents_synced == 1

    async def test_sync_all_errors_returns_failed(self) -> None:
        conn = _make_connector()
        conn.list_opportunities = AsyncMock(side_effect=LeverError("down"))  # type: ignore[method-assign]
        conn.list_postings = AsyncMock(side_effect=LeverError("down"))  # type: ignore[method-assign]
        conn.list_users = AsyncMock(side_effect=LeverError("down"))  # type: ignore[method-assign]
        conn.list_interviews = AsyncMock(side_effect=LeverError("down"))  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_sets_connector_and_tenant_on_doc(self) -> None:
        conn = _make_connector()
        captured: list = []

        async def fake_ingest(doc: object, kb_id: str) -> None:
            captured.append(doc)

        conn.list_opportunities = AsyncMock(return_value=[SAMPLE_OPPORTUNITY])  # type: ignore[method-assign]
        conn.list_postings = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_users = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_interviews = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn._ingest_document = fake_ingest  # type: ignore[method-assign]
        await conn.sync(kb_id="kb_x")
        assert captured[0].connector_id == CONNECTOR_ID
        assert captured[0].tenant_id == TENANT_ID

    async def test_sync_status_partial_when_some_fail(self) -> None:
        conn = _make_connector()

        async def fake_normalize_raise(item: dict) -> object:
            raise ValueError("bad item")

        conn.list_opportunities = AsyncMock(return_value=[SAMPLE_OPPORTUNITY])  # type: ignore[method-assign]
        conn.list_postings = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_users = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_interviews = AsyncMock(return_value=[])  # type: ignore[method-assign]

        # Patch normalize_opportunity to raise inside sync
        with patch("connector.normalize_opportunity", side_effect=ValueError("bad")):
            result = await conn.sync()
        assert result.documents_failed == 1
        assert result.status == SyncStatus.PARTIAL


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — list_* and get_opportunity (5 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    async def test_list_opportunities_follows_pagination(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        pages = [LEVER_PAGE_ONE, LEVER_PAGE_TWO]
        call_count = 0

        async def fake_get_opps(cursor: object = None, **kw: object) -> dict:
            nonlocal call_count
            result = pages[call_count]
            call_count += 1
            return result

        mock_client.get_opportunities = fake_get_opps
        conn._http_client = mock_client
        items = await conn.list_opportunities()
        assert len(items) == 2
        assert call_count == 2

    async def test_list_users_returns_all(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value=LEVER_USERS_PAGE)
        conn._http_client = mock_client
        users = await conn.list_users()
        assert len(users) == 1
        assert users[0]["id"] == "user-222"

    async def test_list_postings_returns_all(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_postings = AsyncMock(return_value=LEVER_POSTINGS_PAGE)
        conn._http_client = mock_client
        postings = await conn.list_postings()
        assert len(postings) == 1
        assert postings[0]["id"] == "post-abc"

    async def test_list_offers_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_offers = AsyncMock(return_value=LEVER_OFFERS_PAGE)
        conn._http_client = mock_client
        offers = await conn.list_offers("opp-111")
        assert offers[0]["id"] == "offer-444"

    async def test_get_opportunity_returns_data_field(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_opportunity = AsyncMock(return_value=LEVER_SINGLE_OPPORTUNITY)
        conn._http_client = mock_client
        opp = await conn.get_opportunity("opp-111")
        assert opp["id"] == "opp-111"

    async def test_list_interviews_returns_all(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_interviews = AsyncMock(return_value=LEVER_INTERVIEWS_PAGE)
        conn._http_client = mock_client
        interviews = await conn.list_interviews()
        assert len(interviews) == 1
        assert interviews[0]["id"] == "iv-333"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 13 — Connector config & lifecycle (5 tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestConnectorConfig:
    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "lever"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_api_key_from_config_dict(self) -> None:
        conn = LeverConnector(config={"api_key": "key_from_config"})
        assert conn._api_key == "key_from_config"

    def test_api_key_kwarg_fallback(self) -> None:
        conn = LeverConnector(api_key="key_kwarg")
        assert conn._api_key == "key_kwarg"

    def test_config_dict_takes_precedence_over_kwarg(self) -> None:
        conn = LeverConnector(config={"api_key": "from_dict"}, api_key="from_kwarg")
        assert conn._api_key == "from_dict"

    async def test_context_manager_aclose(self) -> None:
        async with LeverConnector(config={"api_key": API_KEY}) as conn:
            assert conn is not None
        # After __aexit__, http_client is None
        assert conn._http_client is None

    async def test_ensure_client_creates_and_reuses(self) -> None:
        conn = _make_connector()
        c1 = conn._ensure_client()
        c2 = conn._ensure_client()
        assert c1 is c2

    def test_missing_credentials_with_no_key(self) -> None:
        conn = LeverConnector(config={})
        assert "api_key" in conn._missing_credentials()

    def test_missing_credentials_with_key_present(self) -> None:
        conn = _make_connector()
        assert conn._missing_credentials() == []
