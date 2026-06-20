"""Unit tests for WebexConnector — all Webex HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All 5 exception classes and their attributes
- All model enum values and dataclass fields
- Normalizer functions for rooms, meetings, messages (stable IDs, metadata)
- with_retry (success, retry on network, no retry on auth, exhausted, rate limit)
- WebexHTTPClient (_raise_for_status for 401/403/404/429/500, Bearer header, all endpoints)
- authorize() (returns URL, client_id, scope, redirect_uri)
- install() (success, missing client_id, missing client_secret, with/without token)
- health_check() (healthy with name/email, auth error, network error, missing token)
- sync() (returns SyncResult, counts rooms + meetings, partial graceful)
- list_rooms, list_meetings, list_people, get_room, list_messages
- CircuitBreaker
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

from connector import WebexConnector
from exceptions import (
    WebexAuthError,
    WebexError,
    WebexNetworkError,
    WebexNotFoundError,
    WebexRateLimitError,
)
from helpers.utils import (
    CircuitBreaker,
    _stable_id,
    normalize_meeting,
    normalize_message,
    normalize_room,
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

TENANT_ID = "tenant_test_webex_001"
CONNECTOR_ID = "conn_webex_test_001"
VALID_CLIENT_ID = "webex_client_abc123"
VALID_CLIENT_SECRET = "webex_secret_xyz789"
VALID_ACCESS_TOKEN = "eyJhbGciOiJSUzI1NiJ9.webex_access_token"

# ── Sample fixtures ───────────────────────────────────────────────────────────

SAMPLE_ROOM: dict = {
    "id": "Y2lzY29zcGFyazovL3VzL1JPT00vcm9vbTEyMw",
    "title": "Engineering Team",
    "type": "group",
    "created": "2024-01-15T09:00:00.000Z",
    "lastActivity": "2024-06-01T12:00:00.000Z",
    "isLocked": False,
    "teamId": "team_abc_123",
}

SAMPLE_MEETING: dict = {
    "id": "meeting_id_001",
    "title": "Sprint Planning",
    "start": "2024-06-10T09:00:00Z",
    "end": "2024-06-10T10:00:00Z",
    "timezone": "America/Los_Angeles",
    "meetingType": "scheduledMeeting",
    "status": "scheduled",
    "hostEmail": "host@company.com",
    "webLink": "https://company.webex.com/meet/sprint",
}

SAMPLE_MESSAGE: dict = {
    "id": "msg_id_001",
    "roomId": "Y2lzY29zcGFyazovL3VzL1JPT00vcm9vbTEyMw",
    "text": "Hello, team!",
    "personEmail": "alice@company.com",
    "created": "2024-06-01T10:30:00.000Z",
    "roomType": "group",
}

ROOMS_PAGE: dict = {"items": [SAMPLE_ROOM]}
MEETINGS_PAGE: dict = {"items": [SAMPLE_MEETING]}
MESSAGES_PAGE: dict = {"items": [SAMPLE_MESSAGE]}
EMPTY_ROOMS_PAGE: dict = {"items": []}
EMPTY_MEETINGS_PAGE: dict = {"items": []}

ME_RESPONSE: dict = {
    "id": "person_me_001",
    "displayName": "Alice Smith",
    "emails": ["alice@company.com"],
    "type": "person",
}


# ── Connector fixture ─────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> WebexConnector:
    c = WebexConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert WebexConnector.CONNECTOR_TYPE == "webex"


def test_auth_type_attr() -> None:
    assert WebexConnector.AUTH_TYPE == "oauth2"


def test_connector_stores_tenant_id() -> None:
    c = WebexConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = WebexConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_client_id_from_config() -> None:
    c = WebexConnector(config={"client_id": VALID_CLIENT_ID})
    assert c._client_id == VALID_CLIENT_ID


def test_connector_reads_client_secret_from_config() -> None:
    c = WebexConnector(config={"client_secret": VALID_CLIENT_SECRET})
    assert c._client_secret == VALID_CLIENT_SECRET


def test_connector_reads_access_token_from_config() -> None:
    c = WebexConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._access_token == VALID_ACCESS_TOKEN


def test_connector_reads_redirect_uri_from_config() -> None:
    c = WebexConnector(config={"redirect_uri": "https://app.example.com/callback"})
    assert c._redirect_uri == "https://app.example.com/callback"


def test_connector_no_http_client_initially() -> None:
    c = WebexConnector()
    assert c.http_client is None


def test_connector_default_redirect_uri_empty() -> None:
    c = WebexConnector(config={"client_id": "x", "client_secret": "y"})
    assert c._redirect_uri == ""


def test_connector_reads_refresh_token() -> None:
    c = WebexConnector(config={"refresh_token": "reftok_abc"})
    assert c._refresh_token == "reftok_abc"


def test_connector_reads_token_expires_at() -> None:
    c = WebexConnector(config={"token_expires_at": "2025-01-01T00:00:00Z"})
    assert c._token_expires_at == "2025-01-01T00:00:00Z"


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_webex_error_base() -> None:
    exc = WebexError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_webex_auth_error_is_webex_error() -> None:
    exc = WebexAuthError("auth fail", 401, "UNAUTHORIZED")
    assert isinstance(exc, WebexError)
    assert exc.status_code == 401


def test_webex_auth_error_403() -> None:
    exc = WebexAuthError("Forbidden", 403)
    assert exc.status_code == 403
    assert isinstance(exc, WebexError)


def test_webex_rate_limit_error_attrs() -> None:
    exc = WebexRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_webex_rate_limit_error_default_retry_after() -> None:
    exc = WebexRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_webex_not_found_error_message() -> None:
    exc = WebexNotFoundError("room", "room_xyz")
    assert "room_xyz" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_webex_network_error_is_webex_error() -> None:
    exc = WebexNetworkError("timeout")
    assert isinstance(exc, WebexError)


def test_webex_network_error_default_code() -> None:
    exc = WebexNetworkError("connection reset")
    assert exc.message == "connection reset"
    assert isinstance(exc, WebexError)


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
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="degraded",
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.message == "degraded"


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
        source_url="https://example.com",
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
# 4. STABLE ID + NORMALIZERS
# ════════════════════════════════════════════════════════════════════════


def test_stable_id_format() -> None:
    sid = _stable_id("room:", "room_abc123")
    expected = hashlib.sha256("room:room_abc123".encode()).hexdigest()[:16]
    assert sid == expected
    assert len(sid) == 16


def test_stable_id_is_deterministic() -> None:
    assert _stable_id("room:", "same_id") == _stable_id("room:", "same_id")


def test_stable_id_different_prefixes() -> None:
    assert _stable_id("room:", "abc") != _stable_id("meeting:", "abc")


# normalize_room

def test_normalize_room_title() -> None:
    doc = normalize_room(SAMPLE_ROOM, CONNECTOR_ID, TENANT_ID)
    assert "Engineering Team" in doc.title


def test_normalize_room_source_id_stable_hash() -> None:
    doc = normalize_room(SAMPLE_ROOM, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("room:", SAMPLE_ROOM["id"])
    assert doc.source_id == expected


def test_normalize_room_type_is_room() -> None:
    doc = normalize_room(SAMPLE_ROOM, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "room"


def test_normalize_room_metadata_title() -> None:
    doc = normalize_room(SAMPLE_ROOM, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["title"] == "Engineering Team"


def test_normalize_room_metadata_type() -> None:
    doc = normalize_room(SAMPLE_ROOM, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["type"] == "group"


def test_normalize_room_tenant_connector() -> None:
    doc = normalize_room(SAMPLE_ROOM, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_room_minimal_record() -> None:
    doc = normalize_room({"id": "min_room_001"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("room:", "min_room_001")
    assert "min_room_001" in doc.title


def test_normalize_room_content_has_title() -> None:
    doc = normalize_room(SAMPLE_ROOM, CONNECTOR_ID, TENANT_ID)
    assert "Engineering Team" in doc.content


# normalize_meeting

def test_normalize_meeting_title() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert "Sprint Planning" in doc.title


def test_normalize_meeting_source_id_stable_hash() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("meeting:", SAMPLE_MEETING["id"])
    assert doc.source_id == expected


def test_normalize_meeting_type_is_meeting() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "meeting"


def test_normalize_meeting_metadata_host_email() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["host_email"] == "host@company.com"


def test_normalize_meeting_source_url() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert "webex.com" in doc.source_url


def test_normalize_meeting_tenant_connector() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_meeting_minimal_record() -> None:
    doc = normalize_meeting({"id": "meet_min_001"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("meeting:", "meet_min_001")


def test_normalize_meeting_content_has_title() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert "Sprint Planning" in doc.content


# normalize_message

def test_normalize_message_title_includes_email() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert "alice@company.com" in doc.title


def test_normalize_message_source_id_stable_hash() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("message:", SAMPLE_MESSAGE["id"])
    assert doc.source_id == expected


def test_normalize_message_type_is_message() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "message"


def test_normalize_message_metadata_room_id() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["room_id"] == SAMPLE_MESSAGE["roomId"]


def test_normalize_message_metadata_text() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["text"] == "Hello, team!"


def test_normalize_message_content_has_text() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert "Hello, team!" in doc.content


def test_normalize_message_minimal_record() -> None:
    doc = normalize_message({"id": "msg_min_001"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("message:", "msg_min_001")


def test_normalize_message_tenant_connector() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


# ════════════════════════════════════════════════════════════════════════
# 5. RETRY LOGIC
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_retries_on_network_error() -> None:
    fn = AsyncMock(side_effect=[WebexNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=WebexAuthError("auth fail", 401))
    with pytest.raises(WebexAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=WebexNetworkError("timeout"))
    with pytest.raises(WebexNetworkError):
        await with_retry(fn, max_retries=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_honours_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[WebexRateLimitError("rl", retry_after=0), {"done": True}]
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
# 6. HTTP CLIENT — _raise_for_status
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_bearer_header() -> None:
    """Verify the client attaches a Bearer Authorization header."""
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    session = client._get_session()
    auth_header = session.headers.get("Authorization", "")
    assert auth_header == f"Bearer {VALID_ACCESS_TOKEN}"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_401() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token="bad_token")
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Unauthorized"})
    mock_response.url = "https://webexapis.com/v1/people/me"
    with pytest.raises(WebexAuthError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 401
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_403() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token="bad_token")
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Forbidden"})
    mock_response.url = "https://webexapis.com/v1/rooms"
    with pytest.raises(WebexAuthError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 403
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_404() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Not found"})
    mock_response.url = "https://webexapis.com/v1/rooms/bad_id"
    with pytest.raises(WebexNotFoundError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 404
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_429() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "10"}
    mock_response.json = AsyncMock(return_value={"message": "Too Many Requests"})
    mock_response.url = "https://webexapis.com/v1/rooms"
    with pytest.raises(WebexRateLimitError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 10.0
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_500() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Internal Server Error"})
    mock_response.url = "https://webexapis.com/v1/rooms"
    with pytest.raises(WebexError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 500
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_me_calls_correct_path() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = ME_RESPONSE
        result = await client.get_me()
    mock_req.assert_called_once_with("GET", "/people/me")
    assert result == ME_RESPONSE
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_rooms_passes_max() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = ROOMS_PAGE
        await client.get_rooms(max=50)
    call_params = mock_req.call_args[1]["params"]
    assert call_params["max"] == 50
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_rooms_with_cursor() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = ROOMS_PAGE
        await client.get_rooms(cursor="cursor_abc")
    call_params = mock_req.call_args[1]["params"]
    assert call_params["cursor"] == "cursor_abc"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_room_uses_room_id() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = SAMPLE_ROOM
        await client.get_room("my_room_id")
    mock_req.assert_called_once_with("GET", "/rooms/my_room_id")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_messages_passes_room_id() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = MESSAGES_PAGE
        await client.get_messages(room_id="room_xyz")
    call_params = mock_req.call_args[1]["params"]
    assert call_params["roomId"] == "room_xyz"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_meetings_passes_from_date() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = MEETINGS_PAGE
        await client.get_meetings(from_date="2024-01-01T00:00:00Z")
    call_params = mock_req.call_args[1]["params"]
    assert call_params["from"] == "2024-01-01T00:00:00Z"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_people_with_email() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"items": []}
        await client.get_people(email="alice@company.com")
    call_params = mock_req.call_args[1]["params"]
    assert call_params["email"] == "alice@company.com"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_memberships_with_room_id() -> None:
    from client.http_client import WebexHTTPClient
    client = WebexHTTPClient(access_token=VALID_ACCESS_TOKEN)
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"items": []}
        await client.get_memberships(room_id="room_xyz")
    call_params = mock_req.call_args[1]["params"]
    assert call_params["roomId"] == "room_xyz"
    await client.aclose()


# ════════════════════════════════════════════════════════════════════════
# 7. authorize()
# ════════════════════════════════════════════════════════════════════════


def test_authorize_returns_webex_auth_url() -> None:
    c = WebexConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    url = c.authorize()
    assert "webexapis.com/v1/authorize" in url
    assert VALID_CLIENT_ID in url
    assert "response_type=code" in url


def test_authorize_includes_scope() -> None:
    c = WebexConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    url = c.authorize()
    assert "scope" in url
    assert "spark" in url


def test_authorize_includes_redirect_uri_when_set() -> None:
    c = WebexConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "redirect_uri": "https://app.shielva.ai/callback",
        }
    )
    url = c.authorize()
    assert "redirect_uri" in url
    assert "shielva.ai" in url


def test_authorize_excludes_redirect_uri_when_empty() -> None:
    c = WebexConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    url = c.authorize()
    assert "redirect_uri" not in url


def test_authorize_client_id_in_url() -> None:
    c = WebexConnector(config={"client_id": "my_special_id", "client_secret": "secret"})
    url = c.authorize()
    assert "my_special_id" in url


# ════════════════════════════════════════════════════════════════════════
# 8. install()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    c = WebexConnector(config={"client_secret": VALID_CLIENT_SECRET})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    c = WebexConnector(config={"client_id": VALID_CLIENT_ID})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_secret" in result.message


@pytest.mark.asyncio
async def test_install_no_access_token_returns_healthy() -> None:
    c = WebexConnector(
        config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET}
    )
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "OAuth" in result.message or "flow" in result.message.lower()


@pytest.mark.asyncio
async def test_install_with_access_token_success() -> None:
    c = WebexConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.WebexHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_with_access_token_auth_error() -> None:
    c = WebexConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": "bad_token",
        }
    )
    with patch("connector.WebexHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=WebexAuthError("Invalid token", 401))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_with_access_token_exception_fallback() -> None:
    c = WebexConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        }
    )
    with patch("connector.WebexHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success() -> None:
    c = WebexConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.WebexHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        await c.install()
    assert c.http_client is not None


# ════════════════════════════════════════════════════════════════════════
# 9. health_check()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(authed: WebexConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(return_value=ME_RESPONSE)
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_includes_display_name(authed: WebexConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(return_value=ME_RESPONSE)
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    result = await authed.health_check()
    assert "Alice Smith" in result.message or "alice@company.com" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: WebexConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(side_effect=WebexAuthError("Invalid token", 401))
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: WebexConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(side_effect=WebexNetworkError("timeout"))
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    result = await authed.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = WebexConnector(config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_no_access_token() -> None:
    c = WebexConnector(
        config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET}
    )
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: WebexConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(side_effect=RuntimeError("boom"))
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_increments_circuit_breaker_on_failure(
    authed: WebexConnector,
) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(side_effect=WebexNetworkError("timeout"))
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    await authed.health_check()
    assert authed._circuit_breaker._failures >= 1


@pytest.mark.asyncio
async def test_health_check_resets_circuit_breaker_on_success(
    authed: WebexConnector,
) -> None:
    for _ in range(3):
        authed._circuit_breaker.on_failure()
    instance = MagicMock()
    instance.get_me = AsyncMock(return_value=ME_RESPONSE)
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    await authed.health_check()
    assert authed._circuit_breaker._failures == 0


# ════════════════════════════════════════════════════════════════════════
# 10. sync()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(authed: WebexConnector) -> None:
    authed.http_client.get_rooms = AsyncMock(return_value=EMPTY_ROOMS_PAGE)
    authed.http_client.get_meetings = AsyncMock(return_value=EMPTY_MEETINGS_PAGE)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_rooms_and_meetings(authed: WebexConnector) -> None:
    authed.http_client.get_rooms = AsyncMock(return_value=ROOMS_PAGE)
    authed.http_client.get_meetings = AsyncMock(return_value=MEETINGS_PAGE)
    result = await authed.sync(kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_counts_rooms_and_meetings(authed: WebexConnector) -> None:
    two_rooms = {"items": [SAMPLE_ROOM, {**SAMPLE_ROOM, "id": "room_other_id"}]}
    authed.http_client.get_rooms = AsyncMock(return_value=two_rooms)
    authed.http_client.get_meetings = AsyncMock(return_value=MEETINGS_PAGE)
    result = await authed.sync()
    assert result.documents_found == 3


@pytest.mark.asyncio
async def test_sync_rooms_cursor_pagination(authed: WebexConnector) -> None:
    page1 = {"items": [SAMPLE_ROOM], "nextCursor": "cursor_page2"}
    page2 = {"items": [{**SAMPLE_ROOM, "id": "room_p2"}]}
    authed.http_client.get_rooms = AsyncMock(side_effect=[page1, page2])
    authed.http_client.get_meetings = AsyncMock(return_value=EMPTY_MEETINGS_PAGE)
    result = await authed.sync()
    assert result.documents_found >= 2
    assert authed.http_client.get_rooms.call_count == 2


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(authed: WebexConnector) -> None:
    authed.http_client.get_rooms = AsyncMock(return_value=ROOMS_PAGE)
    authed.http_client.get_meetings = AsyncMock(return_value=EMPTY_MEETINGS_PAGE)
    with patch("connector.normalize_room", side_effect=ValueError("bad data")):
        result = await authed.sync()
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_status_completed_when_no_failures(authed: WebexConnector) -> None:
    authed.http_client.get_rooms = AsyncMock(return_value=ROOMS_PAGE)
    authed.http_client.get_meetings = AsyncMock(return_value=MEETINGS_PAGE)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_rooms_fetch_error_returns_failed(authed: WebexConnector) -> None:
    authed.http_client.get_rooms = AsyncMock(
        side_effect=WebexError("API error", 500)
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_meetings_fetch_error_returns_failed(authed: WebexConnector) -> None:
    authed.http_client.get_rooms = AsyncMock(return_value=EMPTY_ROOMS_PAGE)
    authed.http_client.get_meetings = AsyncMock(
        side_effect=WebexError("API error", 500)
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    c = WebexConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.get_rooms = AsyncMock(return_value=EMPTY_ROOMS_PAGE)
    mock_client.get_meetings = AsyncMock(return_value=EMPTY_MEETINGS_PAGE)
    c._make_client = lambda: mock_client
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_passes_from_date_to_meetings(authed: WebexConnector) -> None:
    authed.http_client.get_rooms = AsyncMock(return_value=EMPTY_ROOMS_PAGE)
    authed.http_client.get_meetings = AsyncMock(return_value=EMPTY_MEETINGS_PAGE)
    await authed.sync(from_date="2024-01-01T00:00:00Z")
    call_kwargs = authed.http_client.get_meetings.call_args
    assert call_kwargs is not None


# ════════════════════════════════════════════════════════════════════════
# 11. list_rooms / get_room
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_rooms_returns_items(authed: WebexConnector) -> None:
    authed.http_client.get_rooms = AsyncMock(return_value=ROOMS_PAGE)
    result = await authed.list_rooms()
    assert result["items"][0]["title"] == "Engineering Team"


@pytest.mark.asyncio
async def test_list_rooms_empty(authed: WebexConnector) -> None:
    authed.http_client.get_rooms = AsyncMock(return_value=EMPTY_ROOMS_PAGE)
    result = await authed.list_rooms()
    assert result["items"] == []


@pytest.mark.asyncio
async def test_list_rooms_with_cursor(authed: WebexConnector) -> None:
    authed.http_client.get_rooms = AsyncMock(return_value=ROOMS_PAGE)
    await authed.list_rooms(cursor="cursor_abc")
    authed.http_client.get_rooms.assert_called_once_with(max=100, cursor="cursor_abc")


@pytest.mark.asyncio
async def test_get_room_returns_room(authed: WebexConnector) -> None:
    authed.http_client.get_room = AsyncMock(return_value=SAMPLE_ROOM)
    result = await authed.get_room(SAMPLE_ROOM["id"])
    assert result["title"] == "Engineering Team"


@pytest.mark.asyncio
async def test_get_room_passes_id(authed: WebexConnector) -> None:
    authed.http_client.get_room = AsyncMock(return_value=SAMPLE_ROOM)
    await authed.get_room("specific_room_id")
    authed.http_client.get_room.assert_called_once_with("specific_room_id")


# ════════════════════════════════════════════════════════════════════════
# 12. list_meetings
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_meetings_returns_items(authed: WebexConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(return_value=MEETINGS_PAGE)
    result = await authed.list_meetings()
    assert result["items"][0]["title"] == "Sprint Planning"


@pytest.mark.asyncio
async def test_list_meetings_empty(authed: WebexConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(return_value=EMPTY_MEETINGS_PAGE)
    result = await authed.list_meetings()
    assert result["items"] == []


@pytest.mark.asyncio
async def test_list_meetings_with_from_date(authed: WebexConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(return_value=MEETINGS_PAGE)
    await authed.list_meetings(from_date="2024-01-01T00:00:00Z")
    authed.http_client.get_meetings.assert_called_once_with(
        cursor=None, from_date="2024-01-01T00:00:00Z"
    )


@pytest.mark.asyncio
async def test_list_meetings_with_cursor(authed: WebexConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(return_value=MEETINGS_PAGE)
    await authed.list_meetings(cursor="cur_abc")
    authed.http_client.get_meetings.assert_called_once_with(
        cursor="cur_abc", from_date=None
    )


# ════════════════════════════════════════════════════════════════════════
# 13. list_people
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_people_returns_items(authed: WebexConnector) -> None:
    people_page = {"items": [{"id": "p1", "displayName": "Bob"}]}
    authed.http_client.get_people = AsyncMock(return_value=people_page)
    result = await authed.list_people()
    assert result["items"][0]["displayName"] == "Bob"


@pytest.mark.asyncio
async def test_list_people_with_email(authed: WebexConnector) -> None:
    authed.http_client.get_people = AsyncMock(return_value={"items": []})
    await authed.list_people(email="bob@company.com")
    authed.http_client.get_people.assert_called_once_with(
        cursor=None, email="bob@company.com"
    )


@pytest.mark.asyncio
async def test_list_people_with_cursor(authed: WebexConnector) -> None:
    authed.http_client.get_people = AsyncMock(return_value={"items": []})
    await authed.list_people(cursor="cur_people")
    authed.http_client.get_people.assert_called_once_with(
        cursor="cur_people", email=None
    )


# ════════════════════════════════════════════════════════════════════════
# 14. list_messages
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_messages_returns_items(authed: WebexConnector) -> None:
    authed.http_client.get_messages = AsyncMock(return_value=MESSAGES_PAGE)
    result = await authed.list_messages(room_id=SAMPLE_ROOM["id"])
    assert result["items"][0]["text"] == "Hello, team!"


@pytest.mark.asyncio
async def test_list_messages_passes_room_id(authed: WebexConnector) -> None:
    authed.http_client.get_messages = AsyncMock(return_value=MESSAGES_PAGE)
    await authed.list_messages(room_id="my_room")
    authed.http_client.get_messages.assert_called_once_with(
        room_id="my_room", max=100, before_message=None
    )


@pytest.mark.asyncio
async def test_list_messages_with_before_message(authed: WebexConnector) -> None:
    authed.http_client.get_messages = AsyncMock(return_value=MESSAGES_PAGE)
    await authed.list_messages(room_id="my_room", before_message="msg_prev_id")
    authed.http_client.get_messages.assert_called_once_with(
        room_id="my_room", max=100, before_message="msg_prev_id"
    )


@pytest.mark.asyncio
async def test_list_messages_empty(authed: WebexConnector) -> None:
    authed.http_client.get_messages = AsyncMock(return_value={"items": []})
    result = await authed.list_messages(room_id="empty_room")
    assert result["items"] == []


# ════════════════════════════════════════════════════════════════════════
# 15. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: WebexConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    c = WebexConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    await c.aclose()
    assert c.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    c = WebexConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    async with c as conn:
        assert conn is c
    mock_client.aclose.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# 16. CircuitBreaker
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


# ════════════════════════════════════════════════════════════════════════
# 17. _ensure_client / _has_credentials
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    c = WebexConnector(config={"access_token": VALID_ACCESS_TOKEN})
    mock_client = MagicMock()
    c._make_client = lambda: mock_client
    client = c._ensure_client()
    assert client is mock_client
    assert c.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    c = WebexConnector(config={"access_token": VALID_ACCESS_TOKEN})
    existing = MagicMock()
    c.http_client = existing
    client = c._ensure_client()
    assert client is existing


def test_has_credentials_true_with_access_token() -> None:
    c = WebexConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._has_credentials() is True


def test_has_credentials_true_with_client_creds() -> None:
    c = WebexConnector(
        config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET}
    )
    assert c._has_credentials() is True


def test_has_credentials_false_when_empty() -> None:
    c = WebexConnector(config={})
    assert c._has_credentials() is False


def test_has_credentials_false_with_only_client_id() -> None:
    c = WebexConnector(config={"client_id": VALID_CLIENT_ID})
    assert c._has_credentials() is False


def test_has_credentials_false_with_only_client_secret() -> None:
    c = WebexConnector(config={"client_secret": VALID_CLIENT_SECRET})
    assert c._has_credentials() is False
