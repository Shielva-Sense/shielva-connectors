"""Unit tests for HeapConnector — all HTTP calls are mocked.

Coverage: 63+ tests across exceptions, models, normalize helpers,
with_retry, HTTP client, install, health_check, sync, list methods,
track_event, and identify_user.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CONNECTOR_TYPE, AUTH_TYPE, HeapConnector
from exceptions import (
    HeapAuthError,
    HeapError,
    HeapNetworkError,
    HeapNotFoundError,
    HeapRateLimitError,
    HeapServerError,
)
from helpers.utils import (
    _stable_id,
    _stable_event_id,
    _stable_user_id,
    normalize_event,
    normalize_segment,
    normalize_user,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HeapEvent,
    HeapResourceType,
    HeapSegment,
    HeapUser,
    SyncStatus,
)

TENANT_ID = "tenant_heap_test"
CONNECTOR_ID = "conn_heap_test_001"
VALID_API_KEY = "heap_bearer_token_xyz"
VALID_ACCOUNT_ID = "3887229184"

# ── Sample data ─────────────────────────────────────────────────────────────

SAMPLE_USERS_RESPONSE: dict = {
    "users": [
        {
            "identity": "user@example.com",
            "properties": {
                "email": "user@example.com",
                "name": "Test User",
                "plan": "pro",
            },
        },
        {
            "identity": "user2@example.com",
            "properties": {
                "email": "user2@example.com",
                "name": "Another User",
            },
        },
    ]
}

SAMPLE_EVENTS_RESPONSE: dict = {
    "events": [
        {
            "event_name": "Button Click",
            "count": 1500,
            "date": "2024-06-01",
        },
        {
            "event_name": "Page View",
            "count": 8200,
            "date": "2024-06-01",
        },
    ]
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

SAMPLE_USER_PROPERTIES_RESPONSE: dict = {
    "identity": "user@example.com",
    "properties": {
        "email": "user@example.com",
        "name": "Test User",
        "plan": "pro",
        "last_seen": "2024-06-01",
    },
}

SAMPLE_TRACK_RESPONSE: dict = {}

SAMPLE_IDENTIFY_RESPONSE: dict = {}

SAMPLE_VALIDATE_RESPONSE: dict = {}


def _make_connector(api_key: str = VALID_API_KEY, account_id: str = VALID_ACCOUNT_ID) -> HeapConnector:
    return HeapConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key, "account_id": account_id},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTIONS (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_heap_error_base(self) -> None:
        exc = HeapError("base error", status_code=400, code="bad_request")
        assert str(exc) == "base error"
        assert exc.message == "base error"
        assert exc.status_code == 400
        assert exc.code == "bad_request"

    def test_heap_auth_error_inherits(self) -> None:
        exc = HeapAuthError("auth failed", status_code=401)
        assert isinstance(exc, HeapError)
        assert exc.status_code == 401

    def test_heap_network_error(self) -> None:
        exc = HeapNetworkError("connection refused")
        assert isinstance(exc, HeapError)
        assert "connection refused" in str(exc)

    def test_heap_not_found_error(self) -> None:
        exc = HeapNotFoundError("user", "user@example.com")
        assert isinstance(exc, HeapError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "user@example.com" in str(exc)

    def test_heap_rate_limit_error(self) -> None:
        exc = HeapRateLimitError("rate limited", retry_after=30.0)
        assert isinstance(exc, HeapError)
        assert exc.status_code == 429
        assert exc.retry_after == 30.0
        assert exc.code == "rate_limit"

    def test_heap_server_error(self) -> None:
        exc = HeapServerError("internal error", status_code=500)
        assert isinstance(exc, HeapError)
        assert exc.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MODELS (6 tests)
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

    def test_heap_user_to_dict(self) -> None:
        user = HeapUser(
            identity="user@test.com",
            properties={"plan": "pro"},
            account_id="12345",
        )
        d = user.to_dict()
        assert d["identity"] == "user@test.com"
        assert d["properties"]["plan"] == "pro"
        assert d["account_id"] == "12345"

    def test_heap_event_to_dict(self) -> None:
        evt = HeapEvent(
            event_name="Button Click",
            identity="user@test.com",
            timestamp="2024-06-01T00:00:00Z",
        )
        d = evt.to_dict()
        assert d["event_name"] == "Button Click"
        assert d["identity"] == "user@test.com"

    def test_heap_segment_to_dict(self) -> None:
        seg = HeapSegment(segment_id="seg_1", name="Power Users", count=500)
        d = seg.to_dict()
        assert d["segment_id"] == "seg_1"
        assert d["name"] == "Power Users"
        assert d["count"] == 500

    def test_heap_resource_type_values(self) -> None:
        assert HeapResourceType.USER == "user"
        assert HeapResourceType.EVENT == "event"
        assert HeapResourceType.SEGMENT == "segment"
        assert HeapResourceType.FUNNEL == "funnel"
        assert HeapResourceType.USER_PROPERTY == "user_property"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZE HELPERS (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeHelpers:
    def test_stable_user_id_deterministic(self) -> None:
        id1 = _stable_user_id("user@example.com")
        id2 = _stable_user_id("user@example.com")
        assert id1 == id2
        assert len(id1) == 16

    def test_stable_user_id_different_identities(self) -> None:
        id1 = _stable_user_id("user@example.com")
        id2 = _stable_user_id("other@example.com")
        assert id1 != id2

    def test_stable_event_id(self) -> None:
        eid = _stable_event_id("12345", "Button Click", "2024-06-01")
        assert len(eid) == 16

    def test_normalize_user_full(self) -> None:
        raw = {
            "identity": "user@example.com",
            "properties": {
                "email": "user@example.com",
                "name": "Test User",
                "plan": "pro",
            },
        }
        doc = normalize_user(raw, VALID_ACCOUNT_ID, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert doc.tenant_id == TENANT_ID
        assert doc.connector_id == CONNECTOR_ID
        assert "user@example.com" in doc.title
        assert "Test User" in doc.content
        assert doc.metadata["resource_type"] == "user"
        assert doc.metadata["email"] == "user@example.com"

    def test_normalize_user_minimal(self) -> None:
        raw = {"identity": "anon_123"}
        doc = normalize_user(raw, VALID_ACCOUNT_ID)
        assert "anon_123" in doc.title
        assert len(doc.source_id) == 16

    def test_normalize_event_full(self) -> None:
        raw = {
            "event_name": "Button Click",
            "count": 1500,
            "date": "2024-06-01",
        }
        doc = normalize_event(raw, VALID_ACCOUNT_ID, CONNECTOR_ID, TENANT_ID)
        assert "Button Click" in doc.title
        assert "1500" in doc.content
        assert doc.metadata["resource_type"] == "event"
        assert doc.metadata["count"] == 1500

    def test_normalize_event_minimal(self) -> None:
        raw = {"event_name": "Page View"}
        doc = normalize_event(raw, VALID_ACCOUNT_ID)
        assert "Page View" in doc.title
        assert len(doc.source_id) == 16

    def test_normalize_segment_full(self) -> None:
        raw = {
            "id": "seg_001",
            "name": "Power Users",
            "description": "Users with >10 sessions",
            "count": 4200,
        }
        doc = normalize_segment(raw, CONNECTOR_ID, TENANT_ID)
        assert "Power Users" in doc.title
        assert "seg_001" in doc.content
        assert "4200" in doc.content
        assert doc.metadata["resource_type"] == "segment"
        assert doc.metadata["count"] == 4200

    def test_normalize_segment_minimal(self) -> None:
        raw = {"name": "Minimal Segment"}
        doc = normalize_segment(raw)
        assert "Minimal Segment" in doc.title


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
    async def test_with_retry_retries_on_heap_error(self) -> None:
        mock_fn = AsyncMock(
            side_effect=[HeapError("fail"), HeapError("fail"), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(mock_fn, max_attempts=3)
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_with_retry_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=HeapError("persistent"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(HeapError, match="persistent"):
                await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.await_count == 3

    @pytest.mark.asyncio
    async def test_with_retry_no_retry_on_auth_error(self) -> None:
        mock_fn = AsyncMock(side_effect=HeapAuthError("auth denied"))
        with pytest.raises(HeapAuthError):
            await with_retry(mock_fn, max_attempts=3)
        mock_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_with_retry_rate_limit_uses_retry_after(self) -> None:
        exc = HeapRateLimitError("too many requests", retry_after=5.0)
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
            side_effect=[HeapNetworkError("timeout"), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(mock_fn, max_attempts=3)
        assert result == {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HTTP CLIENT (14 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHeapHTTPClient:
    def _make_client(self) -> "HeapHTTPClient":  # noqa: F821
        from client.http_client import HeapHTTPClient
        return HeapHTTPClient(
            config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID}
        )

    @pytest.mark.asyncio
    async def test_bearer_auth_header_set(self) -> None:
        client = self._make_client()
        session = client._get_session()
        auth_header = session.headers.get("Authorization", "")
        await client.aclose()
        assert auth_header == f"Bearer {VALID_API_KEY}"

    def test_account_id_stored(self) -> None:
        client = self._make_client()
        assert client._account_id == VALID_ACCOUNT_ID

    @pytest.mark.asyncio
    async def test_validate_credentials_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_VALIDATE_RESPONSE
            result = await client.validate_credentials()
        assert result == SAMPLE_VALIDATE_RESPONSE
        mock_req.assert_awaited_once()
        call_kwargs = mock_req.call_args
        assert call_kwargs[0][1] == "track"

    @pytest.mark.asyncio
    async def test_get_users_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_USERS_RESPONSE
            result = await client.get_users(page=0, limit=50)
        assert result == SAMPLE_USERS_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[0][1] == "users"
        assert call_kwargs[1]["params"]["limit"] == 50

    @pytest.mark.asyncio
    async def test_get_events_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_EVENTS_RESPONSE
            result = await client.get_events(event_name="Button Click", time_range_days=7)
        assert result == SAMPLE_EVENTS_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[0][1] == "events"
        assert call_kwargs[1]["params"]["event_name"] == "Button Click"

    @pytest.mark.asyncio
    async def test_get_events_no_filter(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_EVENTS_RESPONSE
            result = await client.get_events()
        call_kwargs = mock_req.call_args
        assert "event_name" not in call_kwargs[1]["params"]

    @pytest.mark.asyncio
    async def test_get_segments_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_SEGMENTS_RESPONSE
            result = await client.get_segments()
        assert result == SAMPLE_SEGMENTS_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[0][1] == "segments"

    @pytest.mark.asyncio
    async def test_get_user_properties_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_USER_PROPERTIES_RESPONSE
            result = await client.get_user_properties("user@example.com")
        assert result == SAMPLE_USER_PROPERTIES_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[0][1] == "user_properties"
        assert call_kwargs[1]["params"]["identity"] == "user@example.com"

    @pytest.mark.asyncio
    async def test_track_event_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_TRACK_RESPONSE
            result = await client.track_event(
                "user@example.com", "Button Click", {"button_id": "cta_1"}
            )
        assert result == SAMPLE_TRACK_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[0][1] == "track"
        body = call_kwargs[1]["json_body"]
        assert body["identity"] == "user@example.com"
        assert body["event"] == "Button Click"
        assert body["properties"]["button_id"] == "cta_1"
        assert body["app_id"] == VALID_ACCOUNT_ID

    @pytest.mark.asyncio
    async def test_identify_user_success(self) -> None:
        client = self._make_client()
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_IDENTIFY_RESPONSE
            result = await client.identify_user(
                "user@example.com", {"plan": "enterprise"}
            )
        assert result == SAMPLE_IDENTIFY_RESPONSE
        call_kwargs = mock_req.call_args
        assert call_kwargs[0][1] == "identify"
        body = call_kwargs[1]["json_body"]
        assert body["identity"] == "user@example.com"
        assert body["properties"]["plan"] == "enterprise"
        assert body["app_id"] == VALID_ACCOUNT_ID

    @pytest.mark.asyncio
    async def test_raise_for_status_401(self) -> None:
        client = self._make_client()
        with pytest.raises(HeapAuthError):
            client._raise_for_status(401, "Unauthorized", "", "track")

    @pytest.mark.asyncio
    async def test_raise_for_status_403(self) -> None:
        client = self._make_client()
        with pytest.raises(HeapAuthError):
            client._raise_for_status(403, "Forbidden", "", "users")

    @pytest.mark.asyncio
    async def test_raise_for_status_404(self) -> None:
        client = self._make_client()
        with pytest.raises(HeapNotFoundError):
            client._raise_for_status(404, "Not found", "", "segments/999")

    @pytest.mark.asyncio
    async def test_raise_for_status_429(self) -> None:
        client = self._make_client()
        with pytest.raises(HeapRateLimitError):
            client._raise_for_status(429, "Too many requests", "rate_limit", "track")

    @pytest.mark.asyncio
    async def test_raise_for_status_500(self) -> None:
        client = self._make_client()
        with pytest.raises(HeapServerError):
            client._raise_for_status(500, "Internal error", "", "events")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. INSTALL (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    @pytest.mark.asyncio
    async def test_install_success(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.validate_credentials = AsyncMock(return_value={})
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert VALID_ACCOUNT_ID in result.message

    @pytest.mark.asyncio
    async def test_install_missing_api_key(self) -> None:
        connector = _make_connector(api_key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_account_id(self) -> None:
        connector = _make_connector(account_id="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "account_id" in result.message

    @pytest.mark.asyncio
    async def test_install_invalid_credentials(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.validate_credentials = AsyncMock(
                side_effect=HeapAuthError("Invalid API key", 401)
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
            mock_client.validate_credentials = AsyncMock(
                side_effect=HeapNetworkError("Connection refused")
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
            mock_client.validate_credentials = AsyncMock(return_value={})
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.install()
        assert result.connector_id == CONNECTOR_ID


# ═══════════════════════════════════════════════════════════════════════════════
# 7. HEALTH CHECK (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_healthy(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.validate_credentials = AsyncMock(return_value={})
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_health_check_missing_credentials(self) -> None:
        connector = _make_connector(api_key="", account_id="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_missing_account_id(self) -> None:
        connector = _make_connector(account_id="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.validate_credentials = AsyncMock(
                side_effect=HeapAuthError("Invalid token", 401)
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
            mock_client.validate_credentials = AsyncMock(
                side_effect=HeapNetworkError("Timeout")
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
            mock_client.validate_credentials = AsyncMock(
                side_effect=Exception("Unexpected")
            )
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 8. SYNC (9 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _mock_client(self) -> MagicMock:
        mc = MagicMock()
        mc.get_users = AsyncMock(return_value=SAMPLE_USERS_RESPONSE)
        mc.get_events = AsyncMock(return_value=SAMPLE_EVENTS_RESPONSE)
        mc.get_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
        mc.validate_credentials = AsyncMock(return_value={})
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
    async def test_sync_counts_users_and_events(self) -> None:
        connector = _make_connector()
        connector.client = self._mock_client()
        result = await connector.sync()
        # 2 users + 2 events + 2 segments = 6 found
        assert result.documents_found == 6
        assert result.documents_synced == 6

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
        mc.get_users = AsyncMock(side_effect=HeapError("API error"))
        mc.get_events = AsyncMock(return_value=SAMPLE_EVENTS_RESPONSE)
        mc.get_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
        connector.client = mc
        result = await connector.sync()
        # Users skipped, events + segments succeed
        assert result.documents_found >= 2

    @pytest.mark.asyncio
    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        connector = _make_connector()
        connector.client = self._mock_client()
        ingest_mock = AsyncMock()
        connector._ingest_document = ingest_mock
        await connector.sync(kb_id="kb_test_123")
        assert ingest_mock.await_count == 6

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
        mc.get_events = AsyncMock(return_value={"events": []})
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
        mc.get_users = AsyncMock(side_effect=HeapError("fail"))
        mc.get_events = AsyncMock(side_effect=HeapError("fail"))
        mc.get_segments = AsyncMock(side_effect=HeapError("fail"))
        connector.client = mc
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════════════
# 9. LIST METHODS (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    @pytest.mark.asyncio
    async def test_list_users_returns_list(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_users = AsyncMock(return_value=SAMPLE_USERS_RESPONSE)
        connector.client = mc
        users = await connector.list_users()
        assert len(users) == 2
        assert users[0]["identity"] == "user@example.com"

    @pytest.mark.asyncio
    async def test_list_users_pagination(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_users = AsyncMock(return_value={"users": []})
        connector.client = mc
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            users = await connector.list_users(page=2, limit=10)
        mc.get_users.assert_awaited_once_with(page=2, limit=10)
        assert users == []

    @pytest.mark.asyncio
    async def test_list_events_returns_list(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_events = AsyncMock(return_value=SAMPLE_EVENTS_RESPONSE)
        connector.client = mc
        events = await connector.list_events()
        assert len(events) == 2
        assert events[0]["event_name"] == "Button Click"

    @pytest.mark.asyncio
    async def test_list_events_with_filter(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.get_events = AsyncMock(return_value={"events": [{"event_name": "Login"}]})
        connector.client = mc
        events = await connector.list_events(event_name="Login", time_range_days=7)
        mc.get_events.assert_awaited_once_with(event_name="Login", time_range_days=7)
        assert events[0]["event_name"] == "Login"

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


# ═══════════════════════════════════════════════════════════════════════════════
# 10. TRACK EVENT & IDENTIFY (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrackAndIdentify:
    @pytest.mark.asyncio
    async def test_track_event_success(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.track_event = AsyncMock(return_value={})
        connector.client = mc
        result = await connector.track_event(
            "user@example.com", "Button Click", {"button": "cta"}
        )
        mc.track_event.assert_awaited_once_with(
            "user@example.com", "Button Click", {"button": "cta"}
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_track_event_no_properties(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.track_event = AsyncMock(return_value={})
        connector.client = mc
        result = await connector.track_event("anon_user", "Page View")
        mc.track_event.assert_awaited_once_with("anon_user", "Page View", None)
        assert result == {}

    @pytest.mark.asyncio
    async def test_identify_user_success(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.identify_user = AsyncMock(return_value={})
        connector.client = mc
        result = await connector.identify_user(
            "user@example.com", {"plan": "enterprise", "company": "Acme"}
        )
        mc.identify_user.assert_awaited_once_with(
            "user@example.com", {"plan": "enterprise", "company": "Acme"}
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_track_event_auth_error_propagates(self) -> None:
        connector = _make_connector()
        mc = MagicMock()
        mc.track_event = AsyncMock(side_effect=HeapAuthError("Invalid key", 401))
        connector.client = mc
        with pytest.raises(HeapAuthError):
            await connector.track_event("user@example.com", "Event")

    @pytest.mark.asyncio
    async def test_identify_user_creates_client_if_none(self) -> None:
        connector = _make_connector()
        assert connector.client is None
        with patch.object(connector, "_make_client") as mock_make:
            mc = MagicMock()
            mc.identify_user = AsyncMock(return_value={})
            mock_make.return_value = mc
            await connector.identify_user("user@example.com", {"plan": "free"})
        mock_make.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 11. CONNECTOR METADATA (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorMetadata:
    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "heap"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_type_class_attr(self) -> None:
        assert HeapConnector.CONNECTOR_TYPE == "heap"

    def test_auth_type_class_attr(self) -> None:
        assert HeapConnector.AUTH_TYPE == "api_key"

    def test_connector_stores_tenant_id(self) -> None:
        c = _make_connector()
        assert c.tenant_id == TENANT_ID

    def test_connector_stores_connector_id(self) -> None:
        c = _make_connector()
        assert c.connector_id == CONNECTOR_ID
