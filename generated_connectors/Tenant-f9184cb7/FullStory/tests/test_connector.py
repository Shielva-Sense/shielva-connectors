"""Unit tests for FullStoryConnector — all HTTP calls are mocked.

Coverage: 65+ tests across exceptions, models, normalize helpers,
with_retry, HTTP client, install, health_check, sync, list methods,
get methods, and connector metadata.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AUTH_TYPE, CONNECTOR_TYPE, FullStoryConnector
from exceptions import (
    FullStoryAuthError,
    FullStoryError,
    FullStoryNetworkError,
    FullStoryNotFoundError,
    FullStoryRateLimitError,
    FullStoryServerError,
)
from helpers.utils import (
    _stable_id,
    normalize_segment,
    normalize_session,
    normalize_user,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    FullStoryResourceType,
    FullStorySegment,
    FullStorySession,
    FullStoryUser,
    SyncStatus,
)

TENANT_ID = "tenant_fullstory_test"
CONNECTOR_ID = "conn_fullstory_test_001"
VALID_API_KEY = "fs_bearer_token_xyz"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_ORG_RESPONSE: dict = {
    "id": "org_abc123",
    "displayName": "Acme Corp",
    "name": "acme_corp",
}

SAMPLE_SESSIONS_RESPONSE: dict = {
    "sessions": [
        {
            "id": "sess_001",
            "uid": "user_uid_001",
            "createdTime": "2024-06-01T10:00:00Z",
            "durationMs": 45000,
            "pageUrl": "https://app.example.com/dashboard",
        },
        {
            "id": "sess_002",
            "uid": "user_uid_002",
            "createdTime": "2024-06-01T11:00:00Z",
            "durationMs": 12000,
            "pageUrl": "https://app.example.com/settings",
        },
    ],
    "nextPageToken": None,
}

SAMPLE_SESSION_RESPONSE: dict = {
    "id": "sess_001",
    "uid": "user_uid_001",
    "createdTime": "2024-06-01T10:00:00Z",
    "durationMs": 45000,
    "pageUrl": "https://app.example.com/dashboard",
}

SAMPLE_USERS_RESPONSE: dict = {
    "users": [
        {
            "uid": "user_uid_001",
            "displayName": "Alice Smith",
            "email": "alice@example.com",
            "properties": {"plan": "pro", "company": "Acme"},
        },
        {
            "uid": "user_uid_002",
            "displayName": "Bob Jones",
            "email": "bob@example.com",
            "properties": {"plan": "free"},
        },
    ],
    "nextPageToken": "tok_page2",
}

SAMPLE_USER_RESPONSE: dict = {
    "uid": "user_uid_001",
    "displayName": "Alice Smith",
    "email": "alice@example.com",
    "properties": {"plan": "pro"},
}

SAMPLE_SEGMENTS_RESPONSE: dict = {
    "segments": [
        {
            "id": "seg_001",
            "name": "Power Users",
            "description": "Users with >10 sessions/week",
            "count": 4200,
        },
        {
            "id": "seg_002",
            "name": "Churned",
            "description": "No activity in 30 days",
            "count": 1100,
        },
    ]
}

SAMPLE_EVENTS_RESPONSE: dict = {
    "events": [
        {
            "id": "evt_001",
            "name": "Button Click",
            "timestamp": "2024-06-01T10:05:00Z",
            "properties": {"buttonId": "cta_buy"},
        },
        {
            "id": "evt_002",
            "name": "Page View",
            "timestamp": "2024-06-01T10:01:00Z",
            "properties": {"page": "/pricing"},
        },
    ]
}


def _make_connector(api_key: str = VALID_API_KEY) -> FullStoryConnector:
    return FullStoryConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTIONS (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_fullstory_error_base(self) -> None:
        exc = FullStoryError("base error", status_code=400, code="bad_request")
        assert str(exc) == "base error"
        assert exc.message == "base error"
        assert exc.status_code == 400
        assert exc.code == "bad_request"

    def test_fullstory_auth_error_inherits(self) -> None:
        exc = FullStoryAuthError("auth failed", status_code=401)
        assert isinstance(exc, FullStoryError)
        assert exc.status_code == 401

    def test_fullstory_network_error(self) -> None:
        exc = FullStoryNetworkError("connection refused")
        assert isinstance(exc, FullStoryError)
        assert "connection refused" in str(exc)

    def test_fullstory_not_found_error(self) -> None:
        exc = FullStoryNotFoundError("session", "sess_001")
        assert isinstance(exc, FullStoryError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "sess_001" in str(exc)

    def test_fullstory_rate_limit_error(self) -> None:
        exc = FullStoryRateLimitError("rate limited", retry_after=30.0)
        assert isinstance(exc, FullStoryError)
        assert exc.status_code == 429
        assert exc.retry_after == 30.0
        assert exc.code == "rate_limit"

    def test_fullstory_server_error(self) -> None:
        exc = FullStoryServerError("internal error", status_code=500)
        assert isinstance(exc, FullStoryError)
        assert exc.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MODELS (7 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="content",
            connector_id="conn_1",
            tenant_id="tenant_1",
        )
        assert doc.source_id == "abc123"
        assert doc.metadata == {}
        assert doc.source_url == ""

    def test_fullstory_session_to_dict(self) -> None:
        sess = FullStorySession(
            session_id="sess_001",
            uid="user_uid_001",
            created_time="2024-06-01T10:00:00Z",
            duration_ms=45000,
        )
        d = sess.to_dict()
        assert d["session_id"] == "sess_001"
        assert d["uid"] == "user_uid_001"
        assert d["duration_ms"] == 45000

    def test_fullstory_user_to_dict(self) -> None:
        user = FullStoryUser(
            uid="user_uid_001",
            display_name="Alice Smith",
            email="alice@example.com",
            properties={"plan": "pro"},
        )
        d = user.to_dict()
        assert d["uid"] == "user_uid_001"
        assert d["display_name"] == "Alice Smith"
        assert d["email"] == "alice@example.com"
        assert d["properties"]["plan"] == "pro"

    def test_fullstory_segment_to_dict(self) -> None:
        seg = FullStorySegment(
            segment_id="seg_001",
            name="Power Users",
            description="Active users",
            count=4200,
        )
        d = seg.to_dict()
        assert d["segment_id"] == "seg_001"
        assert d["name"] == "Power Users"
        assert d["count"] == 4200

    def test_fullstory_resource_type_values(self) -> None:
        assert FullStoryResourceType.SESSION == "session_recording"
        assert FullStoryResourceType.USER == "user"
        assert FullStoryResourceType.SEGMENT == "segment"
        assert FullStoryResourceType.EVENT == "event"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"
        assert AuthStatus.FAILED == "failed"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZE HELPERS (10 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeHelpers:
    def test_stable_id_deterministic(self) -> None:
        id1 = _stable_id("session:sess_001")
        id2 = _stable_id("session:sess_001")
        assert id1 == id2
        assert len(id1) == 16

    def test_stable_id_different_inputs(self) -> None:
        id1 = _stable_id("session:sess_001")
        id2 = _stable_id("session:sess_002")
        assert id1 != id2

    def test_normalize_session_full(self) -> None:
        raw = {
            "id": "sess_001",
            "uid": "user_uid_001",
            "createdTime": "2024-06-01T10:00:00Z",
            "durationMs": 45000,
            "pageUrl": "https://app.example.com/dashboard",
        }
        doc = normalize_session(raw, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert doc.tenant_id == TENANT_ID
        assert doc.connector_id == CONNECTOR_ID
        assert "sess_001" in doc.title
        assert "user_uid_001" in doc.content
        assert "45000" in doc.content
        assert doc.metadata["resource_type"] == "session_recording"
        assert doc.metadata["session_id"] == "sess_001"
        assert len(doc.source_id) == 16

    def test_normalize_session_minimal(self) -> None:
        raw = {"id": "sess_min"}
        doc = normalize_session(raw)
        assert "sess_min" in doc.title
        assert len(doc.source_id) == 16

    def test_normalize_session_fallback_keys(self) -> None:
        raw = {"sessionId": "sess_alt", "userId": "uid_alt", "duration": 5000}
        doc = normalize_session(raw)
        assert "sess_alt" in doc.title
        assert doc.metadata["uid"] == "uid_alt"
        assert doc.metadata["duration_ms"] == 5000

    def test_normalize_user_full(self) -> None:
        raw = {
            "uid": "user_uid_001",
            "displayName": "Alice Smith",
            "email": "alice@example.com",
            "properties": {"plan": "pro", "company": "Acme"},
        }
        doc = normalize_user(raw, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "alice@example.com" in doc.title
        assert "Alice Smith" in doc.content
        assert doc.metadata["resource_type"] == "user"
        assert doc.metadata["email"] == "alice@example.com"
        assert doc.metadata["uid"] == "user_uid_001"

    def test_normalize_user_minimal(self) -> None:
        raw = {"uid": "anon_uid_123"}
        doc = normalize_user(raw)
        assert "anon_uid_123" in doc.title
        assert len(doc.source_id) == 16

    def test_normalize_user_fallback_keys(self) -> None:
        raw = {"id": "uid_fallback", "name": "Charlie"}
        doc = normalize_user(raw)
        assert doc.metadata["uid"] == "uid_fallback"

    def test_normalize_segment_full(self) -> None:
        raw = {
            "id": "seg_001",
            "name": "Power Users",
            "description": "Users with >10 sessions/week",
            "count": 4200,
        }
        doc = normalize_segment(raw, CONNECTOR_ID, TENANT_ID)
        assert "Power Users" in doc.title
        assert "seg_001" in doc.content
        assert "4200" in doc.content
        assert doc.metadata["resource_type"] == "segment"
        assert doc.metadata["count"] == 4200
        assert doc.metadata["segment_id"] == "seg_001"

    def test_normalize_segment_minimal(self) -> None:
        raw = {"name": "Minimal Segment"}
        doc = normalize_segment(raw)
        assert "Minimal Segment" in doc.title
        assert len(doc.source_id) == 16


# ═══════════════════════════════════════════════════════════════════════════════
# 4. WITH_RETRY (7 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_with_retry_success_first_attempt(self) -> None:
        mock_fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock_fn)
        assert result == {"ok": True}
        mock_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_with_retry_retries_on_fullstory_error(self) -> None:
        mock_fn = AsyncMock(
            side_effect=[FullStoryError("fail"), FullStoryError("fail"), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(mock_fn, max_attempts=3)
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_with_retry_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=FullStoryError("persistent"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(FullStoryError, match="persistent"):
                await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.await_count == 3

    @pytest.mark.asyncio
    async def test_with_retry_no_retry_on_auth_error(self) -> None:
        mock_fn = AsyncMock(side_effect=FullStoryAuthError("auth denied"))
        with pytest.raises(FullStoryAuthError):
            await with_retry(mock_fn, max_attempts=3)
        mock_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_with_retry_rate_limit_uses_retry_after(self) -> None:
        exc = FullStoryRateLimitError("too many requests", retry_after=5.0)
        mock_fn = AsyncMock(side_effect=[exc, {"ok": True}])
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(mock_fn, max_attempts=2)
        assert result == {"ok": True}
        sleep_mock.assert_awaited_once_with(5.0)

    @pytest.mark.asyncio
    async def test_with_retry_passes_args(self) -> None:
        mock_fn = AsyncMock(return_value="data")
        result = await with_retry(mock_fn, "arg1", key="val")
        mock_fn.assert_awaited_once_with("arg1", key="val")
        assert result == "data"

    @pytest.mark.asyncio
    async def test_with_retry_network_error_retried(self) -> None:
        mock_fn = AsyncMock(
            side_effect=[FullStoryNetworkError("timeout"), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(mock_fn, max_attempts=3)
        assert result == {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HTTP CLIENT (14 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFullStoryHTTPClient:
    def _make_client(self) -> "FullStoryHTTPClient":  # noqa: F821
        from client.http_client import FullStoryHTTPClient
        return FullStoryHTTPClient(config={"api_key": VALID_API_KEY})

    @pytest.mark.asyncio
    async def test_bearer_auth_header_set(self) -> None:
        client = self._make_client()
        session = client._get_session()
        auth_header = session.headers.get("Authorization", "")
        await client.aclose()
        assert auth_header == f"Bearer {VALID_API_KEY}"

    def test_api_key_stored(self) -> None:
        client = self._make_client()
        assert client._api_key == VALID_API_KEY

    @pytest.mark.asyncio
    async def test_get_org_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_ORG_RESPONSE
            result = await client.get_org()
        assert result == SAMPLE_ORG_RESPONSE
        mock_req.assert_awaited_once_with("GET", "/v2/org")

    @pytest.mark.asyncio
    async def test_get_sessions_no_filter(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_SESSIONS_RESPONSE
            result = await client.get_sessions(limit=50)
        assert result == SAMPLE_SESSIONS_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[1]["params"]["limit"] == 50
        assert "uid" not in call_kwargs[1]["params"]

    @pytest.mark.asyncio
    async def test_get_sessions_with_uid(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_SESSIONS_RESPONSE
            await client.get_sessions(uid="user_uid_001", limit=10)
        call_kwargs = mock_req.call_args
        assert call_kwargs[1]["params"]["uid"] == "user_uid_001"

    @pytest.mark.asyncio
    async def test_get_sessions_with_cursor(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_SESSIONS_RESPONSE
            await client.get_sessions(cursor="tok_page2")
        call_kwargs = mock_req.call_args
        assert call_kwargs[1]["params"]["pageToken"] == "tok_page2"

    @pytest.mark.asyncio
    async def test_get_session_by_id(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_SESSION_RESPONSE
            result = await client.get_session("sess_001")
        assert result == SAMPLE_SESSION_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[0][1] == "/v2/sessions/sess_001"

    @pytest.mark.asyncio
    async def test_get_users_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_USERS_RESPONSE
            result = await client.get_users(limit=50)
        assert result == SAMPLE_USERS_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[1]["params"]["limit"] == 50

    @pytest.mark.asyncio
    async def test_get_users_with_cursor(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_USERS_RESPONSE
            await client.get_users(cursor="tok_page2")
        call_kwargs = mock_req.call_args
        assert call_kwargs[1]["params"]["pageToken"] == "tok_page2"

    @pytest.mark.asyncio
    async def test_get_user_by_uid(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_USER_RESPONSE
            result = await client.get_user("user_uid_001")
        assert result == SAMPLE_USER_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[0][1] == "/v2/users/user_uid_001"

    @pytest.mark.asyncio
    async def test_get_segments_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_SEGMENTS_RESPONSE
            result = await client.get_segments(limit=50)
        assert result == SAMPLE_SEGMENTS_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[1]["params"]["limit"] == 50

    @pytest.mark.asyncio
    async def test_get_events_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_EVENTS_RESPONSE
            result = await client.get_events("user_uid_001", limit=50)
        assert result == SAMPLE_EVENTS_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[1]["params"]["uid"] == "user_uid_001"
        assert call_kwargs[1]["params"]["limit"] == 50

    @pytest.mark.asyncio
    async def test_raise_for_status_401(self) -> None:
        client = self._make_client()
        with pytest.raises(FullStoryAuthError):
            client._raise_for_status(401, "Unauthorized", "", "/v2/org")

    @pytest.mark.asyncio
    async def test_raise_for_status_403(self) -> None:
        client = self._make_client()
        with pytest.raises(FullStoryAuthError):
            client._raise_for_status(403, "Forbidden", "", "/v2/users")

    @pytest.mark.asyncio
    async def test_raise_for_status_404(self) -> None:
        client = self._make_client()
        with pytest.raises(FullStoryNotFoundError):
            client._raise_for_status(404, "Not found", "", "/v2/sessions/bad_id")

    @pytest.mark.asyncio
    async def test_raise_for_status_429(self) -> None:
        client = self._make_client()
        with pytest.raises(FullStoryRateLimitError):
            client._raise_for_status(429, "Too many requests", "rate_limit", "/v2/users")

    @pytest.mark.asyncio
    async def test_raise_for_status_500(self) -> None:
        client = self._make_client()
        with pytest.raises(FullStoryServerError):
            client._raise_for_status(500, "Internal error", "", "/v2/org")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. INSTALL (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    @pytest.mark.asyncio
    async def test_install_success(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_org = AsyncMock(return_value=SAMPLE_ORG_RESPONSE)
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Acme Corp" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_api_key(self) -> None:
        connector = _make_connector(api_key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    @pytest.mark.asyncio
    async def test_install_invalid_credentials(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_org = AsyncMock(
                side_effect=FullStoryAuthError("Invalid API key", 401)
            )
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_network_error(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_org = AsyncMock(
                side_effect=FullStoryNetworkError("Connection refused")
            )
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_install_sets_connector_id(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_org = AsyncMock(return_value=SAMPLE_ORG_RESPONSE)
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.install()
        assert result.connector_id == CONNECTOR_ID

    @pytest.mark.asyncio
    async def test_install_generic_error_returns_failed(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_org = AsyncMock(side_effect=Exception("Unexpected"))
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED
        assert "Unexpected" in result.message


# ═══════════════════════════════════════════════════════════════════════════════
# 7. HEALTH CHECK (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_healthy(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_org = AsyncMock(return_value=SAMPLE_ORG_RESPONSE)
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Acme Corp" in result.message

    @pytest.mark.asyncio
    async def test_health_check_missing_api_key(self) -> None:
        connector = _make_connector(api_key="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_org = AsyncMock(
                side_effect=FullStoryAuthError("Invalid token", 401)
            )
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_org = AsyncMock(
                side_effect=FullStoryNetworkError("Timeout")
            )
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_generic_error(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_org = AsyncMock(side_effect=Exception("Unexpected"))
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED

    @pytest.mark.asyncio
    async def test_health_check_org_name_fallback(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_org = AsyncMock(return_value={"name": "backup_name"})
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.health_check()
        assert "backup_name" in result.message


# ═══════════════════════════════════════════════════════════════════════════════
# 8. SYNC (9 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _mock_client(self) -> MagicMock:
        mc = MagicMock()
        mc.get_users = AsyncMock(return_value=SAMPLE_USERS_RESPONSE)
        mc.get_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
        mc.get_org = AsyncMock(return_value=SAMPLE_ORG_RESPONSE)
        mc.aclose = AsyncMock()
        return mc

    @pytest.mark.asyncio
    async def test_sync_returns_result(self) -> None:
        connector = _make_connector()
        connector.client = self._mock_client()
        result = await connector.sync()
        assert result is not None
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    @pytest.mark.asyncio
    async def test_sync_counts_users_and_segments(self) -> None:
        connector = _make_connector()
        connector.client = self._mock_client()
        result = await connector.sync()
        # 2 users + 2 segments = 4 found
        assert result.documents_found == 4
        assert result.documents_synced == 4

    @pytest.mark.asyncio
    async def test_sync_completed_on_no_failures(self) -> None:
        connector = _make_connector()
        connector.client = self._mock_client()
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_failed == 0

    @pytest.mark.asyncio
    async def test_sync_partial_on_api_error(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_users = AsyncMock(side_effect=FullStoryError("API error"))
        mc.get_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
        connector.client = mc
        result = await connector.sync()
        # Users skipped, segments succeed
        assert result.documents_found >= 2

    @pytest.mark.asyncio
    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        connector = _make_connector()
        connector.client = self._mock_client()
        ingest_mock = AsyncMock()
        connector._ingest_document = ingest_mock
        await connector.sync(kb_id="kb_test_123")
        assert ingest_mock.await_count == 4

    @pytest.mark.asyncio
    async def test_sync_no_kb_id_skips_ingest(self) -> None:
        connector = _make_connector()
        connector.client = self._mock_client()
        ingest_mock = AsyncMock()
        connector._ingest_document = ingest_mock
        await connector.sync(kb_id="")
        ingest_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_creates_client_if_none(self) -> None:
        connector = _make_connector()
        assert connector.client is None
        with patch.object(connector, "_make_client") as mock_make:
            mc = self._mock_client()
            mock_make.return_value = mc
            await connector.sync()
        mock_make.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_empty_responses(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_users = AsyncMock(return_value={"users": []})
        mc.get_segments = AsyncMock(return_value={"segments": []})
        connector.client = mc
        result = await connector.sync()
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.status == SyncStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_sync_all_resources_fail(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_users = AsyncMock(side_effect=FullStoryError("fail"))
        mc.get_segments = AsyncMock(side_effect=FullStoryError("fail"))
        connector.client = mc
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════════════
# 9. LIST & GET METHODS (12 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestListAndGetMethods:
    @pytest.mark.asyncio
    async def test_list_users_returns_list(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_users = AsyncMock(return_value=SAMPLE_USERS_RESPONSE)
        connector.client = mc
        users = await connector.list_users()
        assert len(users) == 2
        assert users[0]["uid"] == "user_uid_001"

    @pytest.mark.asyncio
    async def test_list_users_with_cursor(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_users = AsyncMock(return_value={"users": []})
        connector.client = mc
        users = await connector.list_users(limit=10, cursor="tok_page2")
        mc.get_users.assert_awaited_once_with(limit=10, cursor="tok_page2")
        assert users == []

    @pytest.mark.asyncio
    async def test_get_user_by_uid(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
        connector.client = mc
        user = await connector.get_user("user_uid_001")
        mc.get_user.assert_awaited_once_with("user_uid_001")
        assert user["uid"] == "user_uid_001"

    @pytest.mark.asyncio
    async def test_list_sessions_no_uid(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_sessions = AsyncMock(return_value=SAMPLE_SESSIONS_RESPONSE)
        connector.client = mc
        sessions = await connector.list_sessions()
        assert len(sessions) == 2
        assert sessions[0]["id"] == "sess_001"

    @pytest.mark.asyncio
    async def test_list_sessions_with_uid(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_sessions = AsyncMock(return_value={"sessions": [SAMPLE_SESSION_RESPONSE]})
        connector.client = mc
        sessions = await connector.list_sessions(uid="user_uid_001", limit=10)
        mc.get_sessions.assert_awaited_once_with(uid="user_uid_001", limit=10, cursor=None)
        assert len(sessions) == 1

    @pytest.mark.asyncio
    async def test_list_sessions_with_cursor(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_sessions = AsyncMock(return_value={"sessions": []})
        connector.client = mc
        sessions = await connector.list_sessions(cursor="tok_next")
        mc.get_sessions.assert_awaited_once_with(uid=None, limit=100, cursor="tok_next")
        assert sessions == []

    @pytest.mark.asyncio
    async def test_get_session_by_id(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_session = AsyncMock(return_value=SAMPLE_SESSION_RESPONSE)
        connector.client = mc
        session = await connector.get_session("sess_001")
        mc.get_session.assert_awaited_once_with("sess_001")
        assert session["id"] == "sess_001"

    @pytest.mark.asyncio
    async def test_list_segments_returns_list(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
        connector.client = mc
        segments = await connector.list_segments()
        assert len(segments) == 2
        assert segments[0]["name"] == "Power Users"

    @pytest.mark.asyncio
    async def test_list_segments_empty(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_segments = AsyncMock(return_value={"segments": []})
        connector.client = mc
        segments = await connector.list_segments()
        assert segments == []

    @pytest.mark.asyncio
    async def test_list_events_returns_list(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_events = AsyncMock(return_value=SAMPLE_EVENTS_RESPONSE)
        connector.client = mc
        events = await connector.list_events("user_uid_001")
        mc.get_events.assert_awaited_once_with("user_uid_001", limit=100)
        assert len(events) == 2
        assert events[0]["name"] == "Button Click"

    @pytest.mark.asyncio
    async def test_list_events_with_limit(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_events = AsyncMock(return_value={"events": []})
        connector.client = mc
        events = await connector.list_events("user_uid_001", limit=5)
        mc.get_events.assert_awaited_once_with("user_uid_001", limit=5)
        assert events == []

    @pytest.mark.asyncio
    async def test_list_methods_create_client_if_none(self) -> None:
        connector = _make_connector()
        assert connector.client is None
        with patch.object(connector, "_make_client") as mock_make:
            mc = MagicMock()
            mc.get_users = AsyncMock(return_value={"users": []})
            mock_make.return_value = mc
            await connector.list_users()
        mock_make.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 10. CONNECTOR METADATA (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorMetadata:
    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "fullstory"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_type_class_attr(self) -> None:
        assert FullStoryConnector.CONNECTOR_TYPE == "fullstory"

    def test_auth_type_class_attr(self) -> None:
        assert FullStoryConnector.AUTH_TYPE == "api_key"

    def test_connector_stores_tenant_id(self) -> None:
        c = _make_connector()
        assert c.tenant_id == TENANT_ID

    def test_connector_stores_connector_id(self) -> None:
        c = _make_connector()
        assert c.connector_id == CONNECTOR_ID

    def test_connector_stores_api_key(self) -> None:
        c = _make_connector()
        assert c._api_key == VALID_API_KEY

    def test_connector_client_starts_none(self) -> None:
        c = _make_connector()
        assert c.client is None
