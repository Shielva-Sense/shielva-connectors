"""Unit tests for ZoomConnector — all Zoom HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes
- All model enum values and dataclass fields
- Normalizer functions for meetings and recordings (full and minimal records)
- Stable ID generation (SHA-256 prefix)
- Retry logic (success, retry-on-error, auth-error short-circuits, rate limit)
- install() — missing creds, with account_id success, auth error, generic exception, no token success
- authorize() — URL construction, with and without redirect_uri
- health_check() — success, auth error, network error, missing creds, no access token, generic exception
- sync() — empty, meetings+recordings, pagination, normalize failure, COMPLETED vs PARTIAL, FAILED
- list_users, list_meetings, get_meeting
- list_recordings, get_recording
- list_webinars
- aclose / context manager
- CircuitBreaker — threshold, reset, half-open, is_open
- _ensure_client
- _has_credentials
- _basic_auth_header
- ZoomHTTPClient — init, get_token, get_account_info, get_users, get_meetings, get_recordings
- _raise_for_status coverage
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

from connector import ZoomConnector
from exceptions import (
    ZoomAuthError,
    ZoomError,
    ZoomNetworkError,
    ZoomNotFoundError,
    ZoomRateLimitError,
    ZoomServerError,
)
from helpers.utils import CircuitBreaker, _stable_id, normalize_meeting, normalize_recording, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "tenant_test_zoom_001"
CONNECTOR_ID = "conn_zoom_test_001"
VALID_ACCOUNT_ID = "acc_abc123"
VALID_CLIENT_ID = "abc123clientid"
VALID_CLIENT_SECRET = "supersecretvalue"
VALID_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.zoom_access_token"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_USER: dict = {
    "id": "user_abc123",
    "email": "alice@example.com",
    "first_name": "Alice",
    "last_name": "Smith",
    "type": 2,
    "status": "active",
    "timezone": "America/New_York",
    "dept": "Engineering",
    "created_at": "2023-01-15T10:00:00Z",
    "last_login_time": "2026-06-19T08:00:00Z",
}

SAMPLE_MEETING: dict = {
    "id": 123456789,
    "topic": "Weekly Sync",
    "status": "waiting",
    "start_time": "2024-06-01T10:00:00Z",
    "duration": 60,
    "timezone": "America/New_York",
    "host_id": "host_abc123",
    "host_email": "host@example.com",
    "join_url": "https://zoom.us/j/123456789",
    "created_at": "2024-05-01T09:00:00Z",
}

SAMPLE_RECORDING: dict = {
    "id": "rec_uuid_001",
    "uuid": "rec_uuid_001",
    "topic": "Board Meeting Recording",
    "start_time": "2024-05-15T14:00:00Z",
    "duration": 90,
    "host_id": "host_def456",
    "host_email": "board@example.com",
    "share_url": "https://zoom.us/rec/share/abc",
    "recording_count": 2,
    "total_size": 104857600,
}

MEETINGS_PAGE: dict = {"meetings": [SAMPLE_MEETING], "next_page_token": ""}
RECORDINGS_PAGE: dict = {"meetings": [SAMPLE_RECORDING], "next_page_token": ""}
EMPTY_MEETINGS_PAGE: dict = {"meetings": [], "next_page_token": ""}
EMPTY_RECORDINGS_PAGE: dict = {"meetings": [], "next_page_token": ""}
USERS_PAGE: dict = {"users": [SAMPLE_USER], "next_page_token": ""}

ACCOUNT_INFO_RESPONSE: dict = {
    "id": "acc_abc123",
    "account_name": "Shielva Test Corp",
    "account_type": "Business",
}

TOKEN_RESPONSE: dict = {"access_token": VALID_ACCESS_TOKEN, "expires_in": 3600}


# ── Connector fixture ────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> ZoomConnector:
    c = ZoomConnector(
        config={
            "account_id": VALID_ACCOUNT_ID,
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
    assert ZoomConnector.CONNECTOR_TYPE == "zoom"


def test_auth_type_attr() -> None:
    # Server-to-Server OAuth is treated as api_key (no user redirect)
    assert ZoomConnector.AUTH_TYPE == "api_key"


def test_connector_stores_tenant_id() -> None:
    c = ZoomConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = ZoomConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_account_id_from_config() -> None:
    c = ZoomConnector(config={"account_id": VALID_ACCOUNT_ID})
    assert c._account_id == VALID_ACCOUNT_ID


def test_connector_reads_client_id_from_config() -> None:
    c = ZoomConnector(config={"client_id": VALID_CLIENT_ID})
    assert c._client_id == VALID_CLIENT_ID


def test_connector_reads_client_secret_from_config() -> None:
    c = ZoomConnector(config={"client_secret": VALID_CLIENT_SECRET})
    assert c._client_secret == VALID_CLIENT_SECRET


def test_connector_reads_access_token_from_config() -> None:
    c = ZoomConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._access_token == VALID_ACCESS_TOKEN


def test_connector_reads_redirect_uri_from_config() -> None:
    c = ZoomConnector(config={"redirect_uri": "https://app.example.com/callback"})
    assert c._redirect_uri == "https://app.example.com/callback"


def test_connector_no_http_client_initially() -> None:
    c = ZoomConnector()
    assert c.http_client is None


def test_connector_default_redirect_uri_empty() -> None:
    c = ZoomConnector(config={"client_id": "x", "client_secret": "y"})
    assert c._redirect_uri == ""


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_zoom_error_base() -> None:
    exc = ZoomError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_zoom_auth_error_is_zoom_error() -> None:
    exc = ZoomAuthError("auth fail", 401, "UNAUTHORIZED")
    assert isinstance(exc, ZoomError)
    assert exc.status_code == 401


def test_zoom_rate_limit_error_attrs() -> None:
    exc = ZoomRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_zoom_rate_limit_error_default_retry_after() -> None:
    exc = ZoomRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_zoom_not_found_error_message() -> None:
    exc = ZoomNotFoundError("meeting", "123456")
    assert "123456" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_zoom_network_error_is_zoom_error() -> None:
    exc = ZoomNetworkError("timeout")
    assert isinstance(exc, ZoomError)


def test_zoom_server_error_is_zoom_error() -> None:
    exc = ZoomServerError("5xx", status_code=503)
    assert isinstance(exc, ZoomError)
    assert exc.status_code == 503


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
    sid = _stable_id("meeting:", "123456789")
    expected = hashlib.sha256("meeting:123456789".encode()).hexdigest()[:16]
    assert sid == expected
    assert len(sid) == 16


def test_stable_id_is_deterministic() -> None:
    assert _stable_id("meeting:", "999") == _stable_id("meeting:", "999")


def test_normalize_meeting_title() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert "Weekly Sync" in doc.title


def test_normalize_meeting_source_id_is_stable_hash() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("meeting:", str(SAMPLE_MEETING["id"]))
    assert doc.source_id == expected


def test_normalize_meeting_tenant_connector() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_meeting_metadata_object_type() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "meeting"


def test_normalize_meeting_metadata_topic() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["topic"] == "Weekly Sync"


def test_normalize_meeting_metadata_duration() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["duration"] == 60


def test_normalize_meeting_source_url() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert "zoom.us" in doc.source_url


def test_normalize_meeting_content_has_topic() -> None:
    doc = normalize_meeting(SAMPLE_MEETING, CONNECTOR_ID, TENANT_ID)
    assert "Weekly Sync" in doc.content


def test_normalize_meeting_minimal_record() -> None:
    doc = normalize_meeting({"id": 9999, "topic": ""}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("meeting:", "9999")
    assert "Meeting 9999" in doc.title


def test_normalize_recording_title() -> None:
    doc = normalize_recording(SAMPLE_RECORDING, CONNECTOR_ID, TENANT_ID)
    assert "Board Meeting Recording" in doc.title


def test_normalize_recording_source_id_is_stable_hash() -> None:
    doc = normalize_recording(SAMPLE_RECORDING, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("meeting:", str(SAMPLE_RECORDING["id"]))
    assert doc.source_id == expected


def test_normalize_recording_metadata_object_type() -> None:
    doc = normalize_recording(SAMPLE_RECORDING, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "recording"


def test_normalize_recording_metadata_share_url() -> None:
    doc = normalize_recording(SAMPLE_RECORDING, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["share_url"] == "https://zoom.us/rec/share/abc"


def test_normalize_recording_metadata_duration() -> None:
    doc = normalize_recording(SAMPLE_RECORDING, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["duration"] == 90


def test_normalize_recording_source_url() -> None:
    doc = normalize_recording(SAMPLE_RECORDING, CONNECTOR_ID, TENANT_ID)
    assert "zoom.us" in doc.source_url


def test_normalize_recording_minimal_record() -> None:
    doc = normalize_recording({"id": "min_001"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("meeting:", "min_001")


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
async def test_retry_retries_on_zoom_error() -> None:
    fn = AsyncMock(side_effect=[ZoomNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=ZoomAuthError("auth fail", 401))
    with pytest.raises(ZoomAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=ZoomNetworkError("timeout"))
    with pytest.raises(ZoomNetworkError):
        await with_retry(fn, max_retries=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[ZoomRateLimitError("rl", retry_after=0), {"done": True}]
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
# 6. ZoomHTTPClient
# ════════════════════════════════════════════════════════════════════════


def test_http_client_stores_config() -> None:
    from client.http_client import ZoomHTTPClient
    config = {"account_id": VALID_ACCOUNT_ID, "client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET}
    client = ZoomHTTPClient(config=config)
    assert client._account_id == VALID_ACCOUNT_ID
    assert client._client_id == VALID_CLIENT_ID
    assert client._client_secret == VALID_CLIENT_SECRET


@pytest.mark.asyncio
async def test_http_client_get_token_returns_access_token() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    config = {
        "account_id": VALID_ACCOUNT_ID,
        "client_id": VALID_CLIENT_ID,
        "client_secret": VALID_CLIENT_SECRET,
    }
    client = ZoomHTTPClient(config=config)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = TOKEN_RESPONSE
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        token = await client.get_token()
    assert token == VALID_ACCESS_TOKEN
    assert client._access_token == VALID_ACCESS_TOKEN
    assert config["access_token"] == VALID_ACCESS_TOKEN


@pytest.mark.asyncio
async def test_http_client_get_token_raises_auth_error_on_failure() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={
        "account_id": VALID_ACCOUNT_ID,
        "client_id": VALID_CLIENT_ID,
        "client_secret": VALID_CLIENT_SECRET,
    })
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 401
    mock_response.json.return_value = {"reason": "Invalid credentials"}
    mock_response.text = "Invalid credentials"
    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(ZoomAuthError):
            await client.get_token()


@pytest.mark.asyncio
async def test_http_client_get_account_info() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    config = {
        "account_id": VALID_ACCOUNT_ID,
        "client_id": VALID_CLIENT_ID,
        "client_secret": VALID_CLIENT_SECRET,
        "access_token": VALID_ACCESS_TOKEN,
    }
    client = ZoomHTTPClient(config=config)
    # Pre-seed token so _auth_header won't call get_token
    client._access_token = VALID_ACCESS_TOKEN
    import time
    client._token_expires_at = time.monotonic() + 3600
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.content = b'{"id": "acc_abc123"}'
    mock_response.json.return_value = ACCOUNT_INFO_RESPONSE
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        result = await client.get_account_info()
    assert result["id"] == "acc_abc123"


@pytest.mark.asyncio
async def test_http_client_get_users_no_pagination() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    import time
    client._token_expires_at = time.monotonic() + 3600
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.content = b'{"users": []}'
    mock_response.json.return_value = USERS_PAGE
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        result = await client.get_users()
    assert "users" in result


@pytest.mark.asyncio
async def test_http_client_get_users_with_next_page_token() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    import time
    client._token_expires_at = time.monotonic() + 3600
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.content = b'{"users": []}'
    mock_response.json.return_value = {"users": [], "next_page_token": ""}
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response) as mock_req:
        await client.get_users(next_page_token="tok_abc")
    call_kwargs = mock_req.call_args
    assert "next_page_token" in str(call_kwargs)


@pytest.mark.asyncio
async def test_http_client_get_meetings() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    import time
    client._token_expires_at = time.monotonic() + 3600
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.content = b'{"meetings": []}'
    mock_response.json.return_value = MEETINGS_PAGE
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        result = await client.get_meetings(user_id="me")
    assert "meetings" in result


@pytest.mark.asyncio
async def test_http_client_get_recordings() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    import time
    client._token_expires_at = time.monotonic() + 3600
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.content = b'{"meetings": []}'
    mock_response.json.return_value = RECORDINGS_PAGE
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        result = await client.get_recordings(user_id="me", from_date="2024-01-01")
    assert "meetings" in result


# ════════════════════════════════════════════════════════════════════════
# 7. _raise_for_status coverage
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_raise_for_status_401() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    import time
    client._token_expires_at = time.monotonic() + 3600
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 401
    mock_response.content = b'{"message": "Unauthorized"}'
    mock_response.json.return_value = {"message": "Unauthorized"}
    mock_response.text = "Unauthorized"
    mock_response.headers = {}
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(ZoomAuthError) as exc_info:
            await client.get_account_info()
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_raise_for_status_403() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    import time
    client._token_expires_at = time.monotonic() + 3600
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 403
    mock_response.content = b'{"message": "Forbidden"}'
    mock_response.json.return_value = {"message": "Forbidden"}
    mock_response.text = "Forbidden"
    mock_response.headers = {}
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(ZoomAuthError) as exc_info:
            await client.get_account_info()
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_raise_for_status_404() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    import time
    client._token_expires_at = time.monotonic() + 3600
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 404
    mock_response.content = b'{"message": "Not found"}'
    mock_response.json.return_value = {"message": "Not found"}
    mock_response.text = "Not found"
    mock_response.headers = {}
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(ZoomNotFoundError):
            await client.get_account_info()


@pytest.mark.asyncio
async def test_raise_for_status_429() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    import time
    client._token_expires_at = time.monotonic() + 3600
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 429
    mock_response.content = b'{"message": "Rate limited"}'
    mock_response.json.return_value = {"message": "Rate limited"}
    mock_response.text = "Rate limited"
    mock_response.headers = {"Retry-After": "5"}
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(ZoomRateLimitError) as exc_info:
            await client.get_account_info()
    assert exc_info.value.retry_after == 5.0


@pytest.mark.asyncio
async def test_raise_for_status_500() -> None:
    import httpx
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    import time
    client._token_expires_at = time.monotonic() + 3600
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 500
    mock_response.content = b'{"message": "Internal error"}'
    mock_response.json.return_value = {"message": "Internal error"}
    mock_response.text = "Internal error"
    mock_response.headers = {}
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response):
        with pytest.raises(ZoomServerError):
            await client.get_account_info()


# ════════════════════════════════════════════════════════════════════════
# 8. Token refresh on 401 / pagination
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_token_refreshed_when_expired() -> None:
    """When token_expires_at is in the past, get_token is called before the request."""
    from client.http_client import ZoomHTTPClient
    import httpx, time
    config = {
        "account_id": VALID_ACCOUNT_ID,
        "client_id": VALID_CLIENT_ID,
        "client_secret": VALID_CLIENT_SECRET,
        "access_token": "old_token",
    }
    client = ZoomHTTPClient(config=config)
    client._access_token = "old_token"
    client._token_expires_at = time.monotonic() - 100  # expired

    mock_token_resp = MagicMock(spec=httpx.Response)
    mock_token_resp.status_code = 200
    mock_token_resp.json.return_value = TOKEN_RESPONSE

    mock_api_resp = MagicMock(spec=httpx.Response)
    mock_api_resp.status_code = 200
    mock_api_resp.content = b'{"id": "acc"}'
    mock_api_resp.json.return_value = {"id": "acc"}

    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_token_resp):
        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_api_resp):
            await client.get_account_info()

    assert client._access_token == VALID_ACCESS_TOKEN


@pytest.mark.asyncio
async def test_token_not_refreshed_when_valid() -> None:
    """When token is still valid, get_token (POST) is NOT called."""
    from client.http_client import ZoomHTTPClient
    import httpx, time
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    client._token_expires_at = time.monotonic() + 3600

    mock_api_resp = MagicMock(spec=httpx.Response)
    mock_api_resp.status_code = 200
    mock_api_resp.content = b'{"id": "acc"}'
    mock_api_resp.json.return_value = {"id": "acc"}

    with patch.object(client._client, "post", new_callable=AsyncMock) as mock_post:
        with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_api_resp):
            await client.get_account_info()
    mock_post.assert_not_called()


# ════════════════════════════════════════════════════════════════════════
# 9. Pagination with next_page_token
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_pagination_passes_token() -> None:
    import httpx, time
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    client._token_expires_at = time.monotonic() + 3600

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.content = b'{"meetings": []}'
    mock_response.json.return_value = {"meetings": [], "next_page_token": ""}

    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response) as mock_req:
        await client.get_meetings(user_id="me", next_page_token="page2_tok")

    call_kwargs = mock_req.call_args
    assert "page2_tok" in str(call_kwargs)


@pytest.mark.asyncio
async def test_http_client_pagination_omits_empty_token() -> None:
    import httpx, time
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    client._token_expires_at = time.monotonic() + 3600

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.content = b'{"meetings": []}'
    mock_response.json.return_value = {"meetings": [], "next_page_token": ""}

    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response) as mock_req:
        await client.get_meetings(user_id="me", next_page_token="")

    call_kwargs = str(mock_req.call_args)
    # next_page_token param should NOT appear when empty
    assert "next_page_token" not in call_kwargs


@pytest.mark.asyncio
async def test_http_client_recordings_pagination_token() -> None:
    import httpx, time
    from client.http_client import ZoomHTTPClient
    client = ZoomHTTPClient(config={"access_token": VALID_ACCESS_TOKEN})
    client._access_token = VALID_ACCESS_TOKEN
    client._token_expires_at = time.monotonic() + 3600

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.content = b'{"meetings": []}'
    mock_response.json.return_value = {"meetings": [], "next_page_token": ""}

    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=mock_response) as mock_req:
        await client.get_recordings(user_id="me", next_page_token="rec_tok")

    assert "rec_tok" in str(mock_req.call_args)


# ════════════════════════════════════════════════════════════════════════
# 10. install()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    c = ZoomConnector(config={"client_secret": VALID_CLIENT_SECRET})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    c = ZoomConnector(config={"client_id": VALID_CLIENT_ID})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_account_id_returns_healthy() -> None:
    """client_id + client_secret present but no account_id → credentials accepted, OAuth pending."""
    c = ZoomConnector(
        config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET}
    )
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "OAuth" in result.message or "flow" in result.message.lower()


@pytest.mark.asyncio
async def test_install_with_account_id_success() -> None:
    """Full S2S: account_id + client_id + client_secret → token exchange + account probe."""
    c = ZoomConnector(
        config={
            "account_id": VALID_ACCOUNT_ID,
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.ZoomHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(return_value=VALID_ACCESS_TOKEN)
        instance.get_account_info = AsyncMock(return_value=ACCOUNT_INFO_RESPONSE)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_with_account_id_auth_error() -> None:
    c = ZoomConnector(
        config={
            "account_id": VALID_ACCOUNT_ID,
            "client_id": VALID_CLIENT_ID,
            "client_secret": "wrong_secret",
        }
    )
    with patch("connector.ZoomHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(side_effect=ZoomAuthError("Invalid credentials", 401))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_with_account_id_exception_fallback() -> None:
    c = ZoomConnector(
        config={
            "account_id": VALID_ACCOUNT_ID,
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
        }
    )
    with patch("connector.ZoomHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success() -> None:
    c = ZoomConnector(
        config={
            "account_id": VALID_ACCOUNT_ID,
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.ZoomHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(return_value=VALID_ACCESS_TOKEN)
        instance.get_account_info = AsyncMock(return_value=ACCOUNT_INFO_RESPONSE)
        instance.aclose = AsyncMock()
        await c.install()
    assert c.http_client is not None


# ════════════════════════════════════════════════════════════════════════
# 11. authorize()
# ════════════════════════════════════════════════════════════════════════


def test_authorize_returns_zoom_auth_url() -> None:
    c = ZoomConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    url = c.authorize()
    assert "zoom.us/oauth/authorize" in url
    assert VALID_CLIENT_ID in url
    assert "response_type=code" in url


def test_authorize_includes_redirect_uri_when_set() -> None:
    c = ZoomConnector(
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
    c = ZoomConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    url = c.authorize()
    assert "redirect_uri" not in url


# ════════════════════════════════════════════════════════════════════════
# 12. health_check()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(authed: ZoomConnector) -> None:
    with patch("connector.ZoomHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account_info = AsyncMock(return_value=ACCOUNT_INFO_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: ZoomConnector) -> None:
    with patch("connector.ZoomHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account_info = AsyncMock(side_effect=ZoomAuthError("Invalid token", 401))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: ZoomConnector) -> None:
    with patch("connector.ZoomHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account_info = AsyncMock(side_effect=ZoomNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = ZoomConnector(config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_no_access_token() -> None:
    c = ZoomConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: ZoomConnector) -> None:
    with patch("connector.ZoomHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account_info = AsyncMock(side_effect=RuntimeError("boom"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_increments_circuit_breaker_on_failure(
    authed: ZoomConnector,
) -> None:
    with patch("connector.ZoomHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account_info = AsyncMock(side_effect=ZoomNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures >= 1


@pytest.mark.asyncio
async def test_health_check_resets_circuit_breaker_on_success(
    authed: ZoomConnector,
) -> None:
    for _ in range(3):
        authed._circuit_breaker.on_failure()
    with patch("connector.ZoomHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account_info = AsyncMock(return_value=ACCOUNT_INFO_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures == 0


# ════════════════════════════════════════════════════════════════════════
# 13. list_users()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_users(authed: ZoomConnector) -> None:
    authed.http_client.get_users = AsyncMock(return_value=USERS_PAGE)
    result = await authed.list_users()
    assert result["users"][0]["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_list_users_custom_status(authed: ZoomConnector) -> None:
    authed.http_client.get_users = AsyncMock(return_value={"users": [], "next_page_token": ""})
    await authed.list_users(status="inactive", page_size=100)
    authed.http_client.get_users.assert_called_once_with(status="inactive", page_size=100)


# ════════════════════════════════════════════════════════════════════════
# 14. sync()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(authed: ZoomConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(return_value=EMPTY_MEETINGS_PAGE)
    authed.http_client.get_recordings = AsyncMock(return_value=EMPTY_RECORDINGS_PAGE)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_meetings_and_recordings(authed: ZoomConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(return_value=MEETINGS_PAGE)
    authed.http_client.get_recordings = AsyncMock(return_value=RECORDINGS_PAGE)
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_meetings_pagination(authed: ZoomConnector) -> None:
    page1 = {"meetings": [SAMPLE_MEETING], "next_page_token": "tok_abc"}
    page2 = {"meetings": [{**SAMPLE_MEETING, "id": 987654321}], "next_page_token": ""}
    authed.http_client.get_meetings = AsyncMock(side_effect=[page1, page2])
    authed.http_client.get_recordings = AsyncMock(return_value=EMPTY_RECORDINGS_PAGE)
    result = await authed.sync(full=True)
    assert result.documents_found >= 2
    assert authed.http_client.get_meetings.call_count == 2


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(authed: ZoomConnector) -> None:
    """A normalize failure increments documents_failed and status becomes PARTIAL."""
    authed.http_client.get_meetings = AsyncMock(
        return_value={"meetings": [{}], "next_page_token": ""}
    )
    authed.http_client.get_recordings = AsyncMock(return_value=EMPTY_RECORDINGS_PAGE)
    with patch("connector.normalize_meeting", side_effect=ValueError("bad data")):
        result = await authed.sync(full=True)
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_status_completed_when_no_failures(authed: ZoomConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(return_value=MEETINGS_PAGE)
    authed.http_client.get_recordings = AsyncMock(return_value=RECORDINGS_PAGE)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_meetings_fetch_error_returns_failed(authed: ZoomConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(
        side_effect=ZoomError("API gone", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_recordings_fetch_error_returns_failed(authed: ZoomConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(return_value=EMPTY_MEETINGS_PAGE)
    authed.http_client.get_recordings = AsyncMock(
        side_effect=ZoomError("API gone", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    c = ZoomConnector(
        config={
            "account_id": VALID_ACCOUNT_ID,
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.get_meetings = AsyncMock(return_value=EMPTY_MEETINGS_PAGE)
    mock_client.get_recordings = AsyncMock(return_value=EMPTY_RECORDINGS_PAGE)
    c._make_client = lambda: mock_client
    result = await c.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_with_since_date(authed: ZoomConnector) -> None:
    from datetime import datetime as dt
    authed.http_client.get_meetings = AsyncMock(return_value=EMPTY_MEETINGS_PAGE)
    authed.http_client.get_recordings = AsyncMock(return_value=EMPTY_RECORDINGS_PAGE)
    result = await authed.sync(since=dt(2024, 1, 1))
    assert result.status == SyncStatus.COMPLETED
    call_kwargs = authed.http_client.get_recordings.call_args
    assert call_kwargs is not None


@pytest.mark.asyncio
async def test_sync_counts_found_correctly(authed: ZoomConnector) -> None:
    two_meetings = {"meetings": [SAMPLE_MEETING, {**SAMPLE_MEETING, "id": 111}], "next_page_token": ""}
    authed.http_client.get_meetings = AsyncMock(return_value=two_meetings)
    authed.http_client.get_recordings = AsyncMock(return_value=RECORDINGS_PAGE)
    result = await authed.sync(full=True)
    assert result.documents_found == 3  # 2 meetings + 1 recording


# ════════════════════════════════════════════════════════════════════════
# 15. list_meetings / get_meeting
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_meetings(authed: ZoomConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(return_value=MEETINGS_PAGE)
    result = await authed.list_meetings()
    assert result["meetings"][0]["id"] == 123456789


@pytest.mark.asyncio
async def test_list_meetings_with_type(authed: ZoomConnector) -> None:
    authed.http_client.get_meetings = AsyncMock(return_value=MEETINGS_PAGE)
    await authed.list_meetings(user_id="me", meeting_type="live", page_size=50)
    authed.http_client.get_meetings.assert_called_once_with(
        user_id="me", type="live", page_size=50
    )


@pytest.mark.asyncio
async def test_get_meeting(authed: ZoomConnector) -> None:
    authed.http_client.get_meeting = AsyncMock(return_value=SAMPLE_MEETING)
    result = await authed.get_meeting("123456789")
    assert result["topic"] == "Weekly Sync"


# ════════════════════════════════════════════════════════════════════════
# 16. list_recordings / get_recording
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_recordings(authed: ZoomConnector) -> None:
    authed.http_client.get_recordings = AsyncMock(return_value=RECORDINGS_PAGE)
    result = await authed.list_recordings()
    assert result["meetings"][0]["topic"] == "Board Meeting Recording"


@pytest.mark.asyncio
async def test_list_recordings_with_dates(authed: ZoomConnector) -> None:
    authed.http_client.get_recordings = AsyncMock(return_value=RECORDINGS_PAGE)
    await authed.list_recordings(user_id="me", from_date="2024-01-01", to_date="2024-06-30")
    authed.http_client.get_recordings.assert_called_once_with(
        user_id="me", from_date="2024-01-01", to_date="2024-06-30"
    )


@pytest.mark.asyncio
async def test_list_recordings_none_dates_become_empty_strings(authed: ZoomConnector) -> None:
    authed.http_client.get_recordings = AsyncMock(return_value=EMPTY_RECORDINGS_PAGE)
    await authed.list_recordings(from_date=None, to_date=None)
    authed.http_client.get_recordings.assert_called_once_with(
        user_id="me", from_date="", to_date=""
    )


@pytest.mark.asyncio
async def test_get_recording(authed: ZoomConnector) -> None:
    authed.http_client.get_recording = AsyncMock(return_value=SAMPLE_RECORDING)
    result = await authed.get_recording("rec_uuid_001")
    assert result["topic"] == "Board Meeting Recording"


# ════════════════════════════════════════════════════════════════════════
# 17. list_webinars
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_webinars(authed: ZoomConnector) -> None:
    webinars_page = {"webinars": [{"id": "web_001", "topic": "Product Launch Webinar"}]}
    authed.http_client.get_webinars = AsyncMock(return_value=webinars_page)
    result = await authed.list_webinars()
    assert result["webinars"][0]["topic"] == "Product Launch Webinar"


@pytest.mark.asyncio
async def test_list_webinars_custom_user_id(authed: ZoomConnector) -> None:
    authed.http_client.get_webinars = AsyncMock(return_value={"webinars": []})
    await authed.list_webinars(user_id="user_xyz")
    authed.http_client.get_webinars.assert_called_once_with(user_id="user_xyz")


# ════════════════════════════════════════════════════════════════════════
# 18. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: ZoomConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    c = ZoomConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    await c.aclose()
    assert c.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    c = ZoomConnector(
        config={
            "account_id": VALID_ACCOUNT_ID,
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
# 19. CircuitBreaker
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
    assert cb.state == "open"


# ════════════════════════════════════════════════════════════════════════
# 20. _ensure_client / _has_credentials / _basic_auth_header
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    c = ZoomConnector(config={"access_token": VALID_ACCESS_TOKEN})
    mock_client = MagicMock()
    c._make_client = lambda: mock_client
    client = c._ensure_client()
    assert client is mock_client
    assert c.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    c = ZoomConnector(config={"access_token": VALID_ACCESS_TOKEN})
    existing = MagicMock()
    c.http_client = existing
    client = c._ensure_client()
    assert client is existing


def test_has_credentials_true_with_access_token() -> None:
    c = ZoomConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._has_credentials() is True


def test_has_credentials_true_with_s2s_creds() -> None:
    c = ZoomConnector(config={
        "account_id": VALID_ACCOUNT_ID,
        "client_id": VALID_CLIENT_ID,
        "client_secret": VALID_CLIENT_SECRET,
    })
    assert c._has_credentials() is True


def test_has_credentials_true_with_client_creds() -> None:
    c = ZoomConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    assert c._has_credentials() is True


def test_has_credentials_false_when_empty() -> None:
    c = ZoomConnector(config={})
    assert c._has_credentials() is False


def test_has_credentials_false_with_only_client_id() -> None:
    c = ZoomConnector(config={"client_id": VALID_CLIENT_ID})
    assert c._has_credentials() is False


def test_has_credentials_false_with_only_client_secret() -> None:
    c = ZoomConnector(config={"client_secret": VALID_CLIENT_SECRET})
    assert c._has_credentials() is False


def test_basic_auth_header_format() -> None:
    import base64
    c = ZoomConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    header = c._basic_auth_header()
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header[6:]).decode()
    assert decoded == f"{VALID_CLIENT_ID}:{VALID_CLIENT_SECRET}"
