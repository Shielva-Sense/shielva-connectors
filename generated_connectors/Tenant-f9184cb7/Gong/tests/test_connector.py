"""Unit tests for GongConnector — all Gong HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All 5 exception classes and their attributes
- All model enums and dataclass fields
- normalize_call, normalize_user, normalize_transcript (stable IDs, metadata fields)
- _stable_id (SHA-256 prefix)
- with_retry (success, retry on network error, no retry on auth error, exhausted)
- GongHTTPClient (BasicAuth, POST calls body format, cursor pagination via records.cursor,
  all 7 endpoints, _raise_for_status 401/403/404/429/500)
- install() (success, missing access_key, missing access_key_secret)
- health_check() (healthy, auth error, network error)
- sync() (returns SyncResult, counts calls + users, partial on normalize failure,
          partial on user fetch error)
- list_calls (date filter, cursor pagination), list_users, get_call,
  get_call_transcript, list_scorecards (return types, empty)
- aclose / context manager
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import GongConnector
from exceptions import (
    GongAuthError,
    GongError,
    GongNetworkError,
    GongNotFoundError,
    GongRateLimitError,
)
from helpers.utils import (
    _stable_id,
    normalize_call,
    normalize_transcript,
    normalize_user,
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

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "tenant_gong_test_001"
CONNECTOR_ID = "conn_gong_test_001"
VALID_KEY = "test_access_key_abc123"
VALID_SECRET = "test_access_key_secret_xyz789"

# ── Sample fixtures ───────────────────────────────────────────────────────────

SAMPLE_CALL: dict = {
    "id": "call_001",
    "title": "Q4 Discovery Call",
    "started": "2024-06-01T10:00:00Z",
    "duration": 3600,
    "url": "https://app.gong.io/call?id=call_001",
    "parties": [
        {"speakerId": "sp1", "name": "Alice Smith", "emailAddress": "alice@acme.com"},
        {"speakerId": "sp2", "name": "Bob Jones", "emailAddress": "bob@vendor.com"},
    ],
}

SAMPLE_USER: dict = {
    "id": "user_001",
    "name": "Alice Smith",
    "emailAddress": "alice@acme.com",
    "title": "Account Executive",
    "managerId": "mgr_001",
}

SAMPLE_TRANSCRIPT: dict = {
    "callId": "call_001",
    "transcript": [
        {
            "speakerId": "sp1",
            "sentences": [
                {"start": 0.0, "end": 5.0, "text": "Hello, how are you?"},
                {"start": 5.5, "end": 10.0, "text": "Thanks for joining."},
            ],
        },
        {
            "speakerId": "sp2",
            "sentences": [
                {"start": 11.0, "end": 16.0, "text": "Great to be here."},
            ],
        },
    ],
}

CALLS_PAGE_1: dict = {
    "calls": [SAMPLE_CALL],
    "records": {"cursor": "cursor_page_2"},
}
CALLS_PAGE_2: dict = {
    "calls": [{"id": "call_002", "title": "Follow-up", "started": "", "duration": 1800, "url": "", "parties": []}],
    "records": {"cursor": None},
}
CALLS_EMPTY: dict = {"calls": [], "records": {}}

USERS_PAGE_1: dict = {
    "users": [SAMPLE_USER],
    "records": {"cursor": "cursor_users_2"},
}
USERS_PAGE_2: dict = {
    "users": [{"id": "user_002", "name": "Bob Jones", "emailAddress": "bob@vendor.com", "title": "SE", "managerId": ""}],
    "records": {"cursor": None},
}
USERS_EMPTY: dict = {"users": [], "records": {}}

SCORECARDS_RESPONSE: dict = {
    "scorecards": [
        {"id": "sc_001", "name": "Discovery Scorecard"},
        {"id": "sc_002", "name": "Demo Scorecard"},
    ]
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_connector(
    access_key: str = VALID_KEY,
    access_key_secret: str = VALID_SECRET,
) -> GongConnector:
    return GongConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"access_key": access_key, "access_key_secret": access_key_secret},
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Class attributes
# ══════════════════════════════════════════════════════════════════════════════

class TestClassAttributes:
    def test_connector_type(self) -> None:
        assert GongConnector.CONNECTOR_TYPE == "gong"

    def test_auth_type(self) -> None:
        assert GongConnector.AUTH_TYPE == "api_key"

    def test_init_stores_config(self) -> None:
        conn = _make_connector()
        assert conn._access_key == VALID_KEY
        assert conn._access_key_secret == VALID_SECRET
        assert conn.tenant_id == TENANT_ID
        assert conn.connector_id == CONNECTOR_ID

    def test_init_empty_config(self) -> None:
        conn = GongConnector()
        assert conn._access_key == ""
        assert conn._access_key_secret == ""

    def test_http_client_initially_none(self) -> None:
        conn = _make_connector()
        assert conn.http_client is None


# ══════════════════════════════════════════════════════════════════════════════
# 2. Exception classes
# ══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_gong_error_base(self) -> None:
        exc = GongError("base error", status_code=500, code="server_error")
        assert str(exc) == "base error"
        assert exc.message == "base error"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_gong_error_defaults(self) -> None:
        exc = GongError("plain error")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_gong_auth_error_inherits(self) -> None:
        exc = GongAuthError("unauthorized", 401, "unauthorized")
        assert isinstance(exc, GongError)
        assert exc.status_code == 401

    def test_gong_network_error_inherits(self) -> None:
        exc = GongNetworkError("timeout")
        assert isinstance(exc, GongError)
        assert exc.status_code == 0

    def test_gong_rate_limit_error(self) -> None:
        exc = GongRateLimitError("too many requests", retry_after=30.0)
        assert isinstance(exc, GongError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 30.0

    def test_gong_rate_limit_default_retry_after(self) -> None:
        exc = GongRateLimitError("rate limited")
        assert exc.retry_after == 0.0

    def test_gong_not_found_error(self) -> None:
        exc = GongNotFoundError("call", "call_999")
        assert isinstance(exc, GongError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "call_999" in str(exc)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Models
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

    def test_install_result_fields(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="conn_001",
            message="ok",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.connector_id == "conn_001"

    def test_health_check_result_fields(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.INVALID_CREDENTIALS,
            message="bad creds",
        )
        assert r.health == ConnectorHealth.OFFLINE
        assert r.message == "bad creds"

    def test_sync_result_fields(self) -> None:
        r = SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=10,
            documents_synced=10,
            documents_failed=0,
        )
        assert r.documents_found == 10
        assert r.documents_failed == 0

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test Doc",
            content="some content",
            connector_id="conn_x",
            tenant_id="tenant_y",
            source_url="https://example.com",
            metadata={"type": "call"},
        )
        assert doc.source_id == "abc123"
        assert doc.metadata["type"] == "call"

    def test_connector_document_default_metadata(self) -> None:
        doc = ConnectorDocument(
            source_id="x", title="y", content="z",
            connector_id="c", tenant_id="t"
        )
        assert doc.metadata == {}
        assert doc.source_url == ""


# ══════════════════════════════════════════════════════════════════════════════
# 4. Normalizers
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeCall:
    def test_stable_id(self) -> None:
        doc = normalize_call(SAMPLE_CALL)
        expected = hashlib.sha256(b"call:call_001").hexdigest()[:16]
        assert doc.source_id == expected

    def test_type_in_metadata(self) -> None:
        doc = normalize_call(SAMPLE_CALL)
        assert doc.metadata["type"] == "call"

    def test_title(self) -> None:
        doc = normalize_call(SAMPLE_CALL)
        assert "Q4 Discovery Call" in doc.title

    def test_duration_in_metadata(self) -> None:
        doc = normalize_call(SAMPLE_CALL)
        assert doc.metadata["duration"] == 3600

    def test_started_in_metadata(self) -> None:
        doc = normalize_call(SAMPLE_CALL)
        assert doc.metadata["started"] == "2024-06-01T10:00:00Z"

    def test_parties_in_metadata(self) -> None:
        doc = normalize_call(SAMPLE_CALL)
        assert len(doc.metadata["parties"]) == 2

    def test_parties_in_content(self) -> None:
        doc = normalize_call(SAMPLE_CALL)
        assert "Alice Smith" in doc.content or "alice@acme.com" in doc.content

    def test_source_url(self) -> None:
        doc = normalize_call(SAMPLE_CALL)
        assert doc.source_url == "https://app.gong.io/call?id=call_001"

    def test_minimal_call(self) -> None:
        doc = normalize_call({"id": "min_001"})
        assert "min_001" in doc.source_id or len(doc.source_id) == 16
        assert doc.metadata["type"] == "call"

    def test_fallback_title(self) -> None:
        doc = normalize_call({"id": "x_fallback"})
        assert "Gong call" in doc.title or "x_fallback" in doc.title

    def test_stable_id_same_input(self) -> None:
        doc1 = normalize_call(SAMPLE_CALL)
        doc2 = normalize_call(SAMPLE_CALL)
        assert doc1.source_id == doc2.source_id


class TestNormalizeUser:
    def test_stable_id(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        expected = hashlib.sha256(b"user:user_001").hexdigest()[:16]
        assert doc.source_id == expected

    def test_type_in_metadata(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert doc.metadata["type"] == "user"

    def test_name_in_title(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert "Alice Smith" in doc.title

    def test_email_in_metadata(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert doc.metadata["email"] == "alice@acme.com"

    def test_title_field_in_metadata(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert doc.metadata["title"] == "Account Executive"

    def test_manager_id_in_metadata(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert doc.metadata["manager_id"] == "mgr_001"

    def test_minimal_user(self) -> None:
        doc = normalize_user({"id": "u_min"})
        assert doc.metadata["type"] == "user"
        assert len(doc.source_id) == 16

    def test_stable_id_same_input(self) -> None:
        doc1 = normalize_user(SAMPLE_USER)
        doc2 = normalize_user(SAMPLE_USER)
        assert doc1.source_id == doc2.source_id


class TestNormalizeTranscript:
    def test_stable_id(self) -> None:
        doc = normalize_transcript(SAMPLE_TRANSCRIPT, "call_001")
        expected = hashlib.sha256(b"transcript:call_001").hexdigest()[:16]
        assert doc.source_id == expected

    def test_type_in_metadata(self) -> None:
        doc = normalize_transcript(SAMPLE_TRANSCRIPT, "call_001")
        assert doc.metadata["type"] == "transcript"

    def test_call_id_in_metadata(self) -> None:
        doc = normalize_transcript(SAMPLE_TRANSCRIPT, "call_001")
        assert doc.metadata["call_id"] == "call_001"

    def test_content_contains_sentences(self) -> None:
        doc = normalize_transcript(SAMPLE_TRANSCRIPT, "call_001")
        assert "Hello, how are you?" in doc.content
        assert "Great to be here." in doc.content

    def test_segment_count_in_metadata(self) -> None:
        doc = normalize_transcript(SAMPLE_TRANSCRIPT, "call_001")
        assert doc.metadata["segment_count"] == 2

    def test_title_contains_call_id(self) -> None:
        doc = normalize_transcript(SAMPLE_TRANSCRIPT, "call_001")
        assert "call_001" in doc.title

    def test_empty_transcript(self) -> None:
        doc = normalize_transcript({}, "call_empty")
        assert doc.metadata["type"] == "transcript"
        assert len(doc.source_id) == 16

    def test_stable_id_same_call_id(self) -> None:
        doc1 = normalize_transcript(SAMPLE_TRANSCRIPT, "call_001")
        doc2 = normalize_transcript({}, "call_001")
        assert doc1.source_id == doc2.source_id


class TestStableId:
    def test_prefix_call(self) -> None:
        result = _stable_id("call:", "123")
        assert result == hashlib.sha256(b"call:123").hexdigest()[:16]

    def test_prefix_user(self) -> None:
        result = _stable_id("user:", "abc")
        assert result == hashlib.sha256(b"user:abc").hexdigest()[:16]

    def test_length_16(self) -> None:
        assert len(_stable_id("test:", "value")) == 16

    def test_different_prefixes_differ(self) -> None:
        a = _stable_id("call:", "same_id")
        b = _stable_id("user:", "same_id")
        assert a != b


# ══════════════════════════════════════════════════════════════════════════════
# 5. with_retry
# ══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_success_first_attempt(self) -> None:
        mock_fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock_fn)
        assert result == {"ok": True}
        mock_fn.assert_awaited_once()

    async def test_retry_on_network_error(self) -> None:
        mock_fn = AsyncMock(
            side_effect=[GongNetworkError("timeout"), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(mock_fn, max_retries=2)
        assert result == {"ok": True}
        assert mock_fn.await_count == 2

    async def test_no_retry_on_auth_error(self) -> None:
        mock_fn = AsyncMock(side_effect=GongAuthError("unauthorized", 401))
        with pytest.raises(GongAuthError):
            await with_retry(mock_fn)
        mock_fn.assert_awaited_once()

    async def test_exhausted_raises_last_exception(self) -> None:
        err = GongNetworkError("persistent timeout")
        mock_fn = AsyncMock(side_effect=err)
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(GongNetworkError):
                await with_retry(mock_fn, max_retries=3)
        assert mock_fn.await_count == 3

    async def test_rate_limit_retry_with_retry_after(self) -> None:
        mock_fn = AsyncMock(
            side_effect=[GongRateLimitError("rate limited", retry_after=1.0), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_retry(mock_fn, max_retries=2)
        assert result == {"ok": True}
        mock_sleep.assert_awaited_once_with(1.0)

    async def test_rate_limit_exhausted(self) -> None:
        mock_fn = AsyncMock(side_effect=GongRateLimitError("rate limited"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(GongRateLimitError):
                await with_retry(mock_fn, max_retries=2)

    async def test_args_passed_through(self) -> None:
        mock_fn = AsyncMock(return_value=42)
        result = await with_retry(mock_fn, "arg1", key="val")
        mock_fn.assert_awaited_once_with("arg1", key="val")
        assert result == 42


# ══════════════════════════════════════════════════════════════════════════════
# 6. GongHTTPClient
# ══════════════════════════════════════════════════════════════════════════════

class TestGongHTTPClient:
    """Tests for the HTTP client using aiohttp mocking."""

    def _make_mock_response(
        self,
        status: int,
        body: dict | None = None,
        headers: dict | None = None,
    ) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.headers = headers or {}
        resp.json = AsyncMock(return_value=body or {})
        resp.text = AsyncMock(return_value="error text")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    def _make_client(self) -> "GongHTTPClient":
        from client.http_client import GongHTTPClient
        return GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)

    async def test_basic_auth_used(self) -> None:
        """Verify BasicAuth is constructed with the right credentials."""
        from client.http_client import GongHTTPClient
        import aiohttp
        client = GongHTTPClient(access_key="key123", access_key_secret="secret456")
        assert client._auth.login == "key123"
        assert client._auth.password == "secret456"
        await client.aclose()

    async def test_get_users_calls_correct_path(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=USERS_EMPTY) as mock_req:
            result = await client.get_users()
        mock_req.assert_awaited_once_with("GET", "/v2/users", params={})
        assert result == USERS_EMPTY
        await client.aclose()

    async def test_get_users_with_cursor(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=USERS_EMPTY) as mock_req:
            await client.get_users(cursor="cursor_abc")
        mock_req.assert_awaited_once_with("GET", "/v2/users", params={"cursor": "cursor_abc"})
        await client.aclose()

    async def test_get_calls_post_with_filter_body(self) -> None:
        """Gong uses POST /v2/calls with a JSON body for filtering."""
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=CALLS_EMPTY) as mock_req:
            await client.get_calls(from_date="2024-01-01T00:00:00Z", to_date="2024-12-31T23:59:59Z")
        mock_req.assert_awaited_once_with(
            "POST", "/v2/calls",
            json={"filter": {"fromDateTime": "2024-01-01T00:00:00Z", "toDateTime": "2024-12-31T23:59:59Z"}}
        )
        await client.aclose()

    async def test_get_calls_cursor_in_filter(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=CALLS_EMPTY) as mock_req:
            await client.get_calls(cursor="cursor_xyz")
        called_json = mock_req.call_args[1]["json"]
        assert called_json["filter"]["cursor"] == "cursor_xyz"
        await client.aclose()

    async def test_get_calls_no_filter_empty_body(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=CALLS_EMPTY) as mock_req:
            await client.get_calls()
        mock_req.assert_awaited_once_with("POST", "/v2/calls", json={})
        await client.aclose()

    async def test_get_call_by_id(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=SAMPLE_CALL) as mock_req:
            result = await client.get_call("call_001")
        mock_req.assert_awaited_once_with("GET", "/v2/calls/call_001")
        assert result == SAMPLE_CALL
        await client.aclose()

    async def test_get_call_transcripts_post(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={}) as mock_req:
            await client.get_call_transcripts("call_001")
        mock_req.assert_awaited_once_with(
            "POST", "/v2/calls/transcript",
            json={"filter": {"callIds": ["call_001"]}}
        )
        await client.aclose()

    async def test_get_stats(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"data": []}) as mock_req:
            result = await client.get_stats()
        mock_req.assert_awaited_once_with("GET", "/v2/stats/activity/account")
        await client.aclose()

    async def test_get_deals(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value={"deals": []}) as mock_req:
            await client.get_deals(cursor="c1")
        mock_req.assert_awaited_once_with("GET", "/v2/crm/deals", params={"cursor": "c1"})
        await client.aclose()

    async def test_get_scorecards(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=SCORECARDS_RESPONSE) as mock_req:
            result = await client.get_scorecards()
        mock_req.assert_awaited_once_with("GET", "/v2/settings/scorecards")
        assert result == SCORECARDS_RESPONSE
        await client.aclose()

    async def test_raise_for_status_401(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        resp = self._make_mock_response(401, body={"message": "Invalid credentials"})
        with pytest.raises(GongAuthError) as exc_info:
            await client._raise_for_status(resp, "/v2/users")
        assert exc_info.value.status_code == 401
        await client.aclose()

    async def test_raise_for_status_403(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        resp = self._make_mock_response(403, body={"message": "Forbidden"})
        with pytest.raises(GongAuthError) as exc_info:
            await client._raise_for_status(resp, "/v2/calls")
        assert exc_info.value.status_code == 403
        await client.aclose()

    async def test_raise_for_status_404(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        resp = self._make_mock_response(404, body={"message": "Not found"})
        with pytest.raises(GongNotFoundError):
            await client._raise_for_status(resp, "/v2/calls/missing")
        await client.aclose()

    async def test_raise_for_status_429(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        resp = self._make_mock_response(429, headers={"Retry-After": "60"})
        with pytest.raises(GongRateLimitError) as exc_info:
            await client._raise_for_status(resp, "/v2/calls")
        assert exc_info.value.retry_after == 60.0
        await client.aclose()

    async def test_raise_for_status_500(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        resp = self._make_mock_response(500, body={"message": "Internal server error"})
        with pytest.raises(GongError) as exc_info:
            await client._raise_for_status(resp, "/v2/calls")
        assert exc_info.value.status_code == 500
        await client.aclose()

    async def test_raise_for_status_200_returns_body(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        resp = self._make_mock_response(200, body={"users": [SAMPLE_USER]})
        result = await client._raise_for_status(resp, "/v2/users")
        assert result == {"users": [SAMPLE_USER]}
        await client.aclose()

    async def test_raise_for_status_204_returns_empty(self) -> None:
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        resp = self._make_mock_response(204)
        result = await client._raise_for_status(resp, "/v2/calls/del")
        assert result == {}
        await client.aclose()

    async def test_cursor_pagination_via_records_cursor(self) -> None:
        """Records cursor is in response.records.cursor, not response.cursor."""
        from client.http_client import GongHTTPClient
        client = GongHTTPClient(access_key=VALID_KEY, access_key_secret=VALID_SECRET)
        # get_users with cursor should pass it as a query param
        with patch.object(client, "_request", new_callable=AsyncMock, return_value=USERS_PAGE_1) as mock_req:
            result = await client.get_users(cursor=None)
        assert result["records"]["cursor"] == "cursor_users_2"
        await client.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# 7. install()
# ══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    async def test_install_success(self) -> None:
        conn = _make_connector()
        result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    async def test_install_missing_access_key(self) -> None:
        conn = _make_connector(access_key="")
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "access_key" in result.message.lower()

    async def test_install_missing_access_key_secret(self) -> None:
        conn = _make_connector(access_key_secret="")
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "access_key_secret" in result.message.lower()

    async def test_install_both_missing(self) -> None:
        conn = _make_connector(access_key="", access_key_secret="")
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ══════════════════════════════════════════════════════════════════════════════
# 8. health_check()
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    async def test_health_check_healthy(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value=USERS_EMPTY)
        mock_client.aclose = AsyncMock()
        conn._make_client = MagicMock(return_value=mock_client)
        with patch("connector.with_retry", new_callable=AsyncMock, return_value=USERS_EMPTY):
            result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_auth_error(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        conn._make_client = MagicMock(return_value=mock_client)
        with patch("connector.with_retry", new_callable=AsyncMock,
                   side_effect=GongAuthError("bad key", 401)):
            result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        conn._make_client = MagicMock(return_value=mock_client)
        with patch("connector.with_retry", new_callable=AsyncMock,
                   side_effect=GongNetworkError("timeout")):
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_missing_credentials(self) -> None:
        conn = _make_connector(access_key="", access_key_secret="")
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_generic_exception(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        conn._make_client = MagicMock(return_value=mock_client)
        with patch("connector.with_retry", new_callable=AsyncMock,
                   side_effect=RuntimeError("unexpected")):
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ══════════════════════════════════════════════════════════════════════════════
# 9. sync()
# ══════════════════════════════════════════════════════════════════════════════

class TestSync:
    async def test_sync_returns_sync_result(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_calls = AsyncMock(return_value=CALLS_EMPTY)
        mock_client.get_users = AsyncMock(return_value=USERS_EMPTY)
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock,
                   side_effect=[CALLS_EMPTY, USERS_EMPTY]):
            result = await conn.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_counts_calls_and_users(self) -> None:
        conn = _make_connector()

        calls_page = {"calls": [SAMPLE_CALL, SAMPLE_CALL], "records": {}}
        users_page = {"users": [SAMPLE_USER], "records": {}}

        async def mock_retry(fn, *args, **kwargs):
            if fn == conn.http_client.get_calls or (hasattr(fn, '__self__') and hasattr(fn.__self__, 'get_calls')):
                return calls_page
            return users_page

        mock_client = MagicMock()
        mock_client.get_calls = AsyncMock(return_value=calls_page)
        mock_client.get_users = AsyncMock(return_value=users_page)
        conn.http_client = mock_client

        with patch("connector.with_retry") as mock_wr:
            mock_wr.side_effect = [calls_page, users_page]
            result = await conn.sync()

        assert result.documents_found == 3  # 2 calls + 1 user
        assert result.documents_synced == 3
        assert result.documents_failed == 0
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_partial_on_normalize_failure(self) -> None:
        conn = _make_connector()
        # A call with no id should raise when _stable_id is called
        bad_call = None  # normalize_call(None) will raise

        calls_page = {"calls": [SAMPLE_CALL, bad_call], "records": {}}
        users_page = {"users": [], "records": {}}

        mock_client = MagicMock()
        mock_client.get_calls = AsyncMock(return_value=calls_page)
        mock_client.get_users = AsyncMock(return_value=users_page)
        conn.http_client = mock_client

        with patch("connector.with_retry") as mock_wr:
            mock_wr.side_effect = [calls_page, users_page]
            result = await conn.sync()

        # One succeeds, one fails
        assert result.documents_failed >= 1
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_failed_on_call_fetch_error(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock,
                   side_effect=GongError("API down", 503)):
            result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_partial_on_user_fetch_error(self) -> None:
        conn = _make_connector()
        calls_page = {"calls": [SAMPLE_CALL], "records": {}}

        mock_client = MagicMock()
        conn.http_client = mock_client

        with patch("connector.with_retry") as mock_wr:
            mock_wr.side_effect = [calls_page, GongError("user API down")]
            result = await conn.sync()

        assert result.status == SyncStatus.PARTIAL
        assert result.documents_synced == 1  # calls synced before user failure

    async def test_sync_completed_status_when_no_failures(self) -> None:
        conn = _make_connector()
        calls_page = {"calls": [], "records": {}}
        users_page = {"users": [], "records": {}}
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry") as mock_wr:
            mock_wr.side_effect = [calls_page, users_page]
            result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED


# ══════════════════════════════════════════════════════════════════════════════
# 10. list_calls / list_users / get_call / get_call_transcript / list_scorecards
# ══════════════════════════════════════════════════════════════════════════════

class TestListCalls:
    async def test_list_calls_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock,
                   return_value={"calls": [SAMPLE_CALL], "records": {}}):
            result = await conn.list_calls()
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_calls_with_date_filter(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.get_calls = AsyncMock(
            return_value={"calls": [SAMPLE_CALL], "records": {}}
        )
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock,
                   return_value={"calls": [SAMPLE_CALL], "records": {}}) as mock_wr:
            result = await conn.list_calls(from_date="2024-01-01", to_date="2024-12-31")
        # Verify date params were passed (via from_date/to_date kwargs)
        call_kwargs = mock_wr.call_args[1]
        assert call_kwargs.get("from_date") == "2024-01-01"
        assert call_kwargs.get("to_date") == "2024-12-31"

    async def test_list_calls_cursor_pagination(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry") as mock_wr:
            mock_wr.side_effect = [CALLS_PAGE_1, CALLS_PAGE_2]
            result = await conn.list_calls()
        assert len(result) == 2

    async def test_list_calls_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock,
                   return_value=CALLS_EMPTY):
            result = await conn.list_calls()
        assert result == []


class TestListUsers:
    async def test_list_users_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry") as mock_wr:
            mock_wr.side_effect = [USERS_PAGE_1, USERS_PAGE_2]
            result = await conn.list_users()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_users_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock,
                   return_value=USERS_EMPTY):
            result = await conn.list_users()
        assert result == []


class TestGetCall:
    async def test_get_call_returns_dict(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock,
                   return_value=SAMPLE_CALL):
            result = await conn.get_call("call_001")
        assert isinstance(result, dict)
        assert result["id"] == "call_001"


class TestGetCallTranscript:
    async def test_get_call_transcript_returns_dict(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        transcript_response = {"callTranscripts": [SAMPLE_TRANSCRIPT]}
        with patch("connector.with_retry", new_callable=AsyncMock,
                   return_value=transcript_response):
            result = await conn.get_call_transcript("call_001")
        assert isinstance(result, dict)
        assert "callTranscripts" in result

    async def test_get_call_transcript_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock, return_value={}):
            result = await conn.get_call_transcript("call_999")
        assert isinstance(result, dict)


class TestListScorecards:
    async def test_list_scorecards_returns_list(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock,
                   return_value=SCORECARDS_RESPONSE):
            result = await conn.list_scorecards()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_scorecards_empty(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock,
                   return_value={"scorecards": []}):
            result = await conn.list_scorecards()
        assert result == []

    async def test_list_scorecards_missing_key(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        with patch("connector.with_retry", new_callable=AsyncMock, return_value={}):
            result = await conn.list_scorecards()
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# 11. aclose / context manager / _ensure_client
# ══════════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    async def test_aclose_clears_client(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        conn.http_client = mock_client
        await conn.aclose()
        assert conn.http_client is None
        mock_client.aclose.assert_awaited_once()

    async def test_aclose_when_no_client(self) -> None:
        conn = _make_connector()
        await conn.aclose()  # should not raise

    async def test_ensure_client_creates_client(self) -> None:
        conn = _make_connector()
        assert conn.http_client is None
        client = conn._ensure_client()
        assert conn.http_client is not None
        assert client is conn.http_client

    async def test_ensure_client_reuses_existing(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        client = conn._ensure_client()
        assert client is mock_client

    async def test_context_manager(self) -> None:
        conn = _make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        conn.http_client = mock_client
        async with conn as c:
            assert c is conn
        assert conn.http_client is None
