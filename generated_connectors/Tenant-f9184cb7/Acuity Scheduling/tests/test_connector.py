"""Unit tests for AcuitySchedulingConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AcuitySchedulingConnector
from exceptions import (
    AcuityAuthError,
    AcuityError,
    AcuityNetworkError,
    AcuityNotFoundError,
    AcuityRateLimitError,
)
from helpers.utils import (
    normalize_appointment,
    normalize_appointment_type,
    normalize_client,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ─────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_acuity_test_001"
USER_ID = "12345678"
API_KEY = "test-api-key-secret"

SAMPLE_ME_RESPONSE: dict = {
    "id": 12345678,
    "name": "Shielva Demo Business",
    "email": "demo@shielva.ai",
    "timezone": "America/New_York",
    "plan": "growing",
}

SAMPLE_APPOINTMENT: dict = {
    "id": 1001,
    "type": "30 Minute Consultation",
    "firstName": "Alice",
    "lastName": "Smith",
    "email": "alice@example.com",
    "phone": "+1-555-0100",
    "date": "2026-06-25",
    "time": "10:00am",
    "endTime": "10:30am",
    "calendar": "Main Calendar",
    "status": "scheduled",
    "notes": "First consultation",
    "location": "Zoom",
    "timezone": "America/Los_Angeles",
}

SAMPLE_APPOINTMENT_2: dict = {
    "id": 1002,
    "type": "60 Minute Strategy",
    "firstName": "Bob",
    "lastName": "Jones",
    "email": "bob@example.com",
    "phone": "",
    "date": "2026-06-26",
    "time": "2:00pm",
    "endTime": "3:00pm",
    "calendar": "Main Calendar",
    "status": "scheduled",
    "notes": "",
    "location": "",
    "timezone": "America/Chicago",
}

SAMPLE_CLIENT: dict = {
    "id": 5001,
    "firstName": "Alice",
    "lastName": "Smith",
    "email": "alice@example.com",
    "phone": "+1-555-0100",
    "notes": "VIP client",
}

SAMPLE_CLIENT_2: dict = {
    "id": 5002,
    "firstName": "Bob",
    "lastName": "Jones",
    "email": "bob@example.com",
    "phone": "",
    "notes": "",
}

SAMPLE_APPOINTMENT_TYPE: dict = {
    "id": 101,
    "name": "30 Minute Consultation",
    "duration": 30,
    "price": "0.00",
    "category": "Consulting",
    "description": "A free 30-minute intro call.",
    "color": "#5AB5A5",
    "active": True,
}

SAMPLE_APPOINTMENT_TYPE_2: dict = {
    "id": 102,
    "name": "60 Minute Strategy",
    "duration": 60,
    "price": "150.00",
    "category": "Consulting",
    "description": "",
    "color": "#0099CC",
    "active": True,
}

SAMPLE_CALENDAR: dict = {
    "id": 201,
    "name": "Main Calendar",
    "description": "Primary calendar",
    "timezone": "America/New_York",
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> AcuitySchedulingConnector:
    return AcuitySchedulingConnector(
        config={"user_id": USER_ID, "api_key": API_KEY},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: AcuitySchedulingConnector) -> AcuitySchedulingConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_acuity_error_is_exception() -> None:
    exc = AcuityError("base error", status_code=500, code="server_error")
    assert isinstance(exc, Exception)
    assert exc.status_code == 500
    assert exc.code == "server_error"
    assert exc.message == "base error"


def test_acuity_auth_error_inherits_acuity_error() -> None:
    exc = AcuityAuthError("invalid credentials", 401)
    assert isinstance(exc, AcuityError)
    assert exc.status_code == 401


def test_acuity_network_error_inherits_acuity_error() -> None:
    exc = AcuityNetworkError("timeout", 503)
    assert isinstance(exc, AcuityError)
    assert exc.status_code == 503


def test_acuity_not_found_error_inherits_acuity_error() -> None:
    exc = AcuityNotFoundError("appointment", 999)
    assert isinstance(exc, AcuityError)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"
    assert "999" in str(exc)


def test_acuity_rate_limit_error_inherits_acuity_error() -> None:
    exc = AcuityRateLimitError("too many requests", retry_after=30.0)
    assert isinstance(exc, AcuityError)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 30.0


def test_acuity_rate_limit_default_retry_after() -> None:
    exc = AcuityRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_acuity_error_str_repr() -> None:
    exc = AcuityError("something went wrong")
    assert "something went wrong" in str(exc)


# ── Models ────────────────────────────────────────────────────────────────────


def test_connector_health_enum_values() -> None:
    from models import ConnectorHealth
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum_values() -> None:
    from models import AuthStatus
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.FAILED == "failed"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_enum_values() -> None:
    from models import SyncStatus
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_install_result_dataclass() -> None:
    from models import InstallResult
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="conn_1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.connector_id == "conn_1"


def test_health_check_result_dataclass() -> None:
    from models import HealthCheckResult
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="err",
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.message == "err"


def test_sync_result_dataclass() -> None:
    from models import SyncResult
    r = SyncResult(
        status=SyncStatus.PARTIAL,
        documents_found=10,
        documents_synced=8,
        documents_failed=2,
        message="partial",
    )
    assert r.documents_found == 10
    assert r.documents_failed == 2


def test_connector_document_dataclass() -> None:
    from models import ConnectorDocument
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test",
        content="Content",
        connector_id="c1",
        tenant_id="t1",
        source_url="https://example.com",
        metadata={"key": "val"},
    )
    assert doc.source_id == "abc123"
    assert doc.metadata["key"] == "val"


# ── normalize_appointment ─────────────────────────────────────────────────────


def test_normalize_appointment_source_id_is_16_chars() -> None:
    doc = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_appointment_source_id_is_hex() -> None:
    doc = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    int(doc.source_id, 16)  # raises ValueError if not hex


def test_normalize_appointment_source_id_is_deterministic() -> None:
    doc1 = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_appointment_different_ids_produce_different_source_ids() -> None:
    doc1 = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_appointment(SAMPLE_APPOINTMENT_2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_appointment_type_prefix_in_hash() -> None:
    """Ensure appointment hash differs from client hash for same numeric id."""
    appt = {**SAMPLE_APPOINTMENT, "id": 5001}
    appt_doc = normalize_appointment(appt, CONNECTOR_ID, TENANT_ID)
    client_doc = normalize_client(SAMPLE_CLIENT, CONNECTOR_ID, TENANT_ID)
    assert appt_doc.source_id != client_doc.source_id


def test_normalize_appointment_title() -> None:
    doc = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    assert "30 Minute Consultation" in doc.title
    assert "Alice Smith" in doc.title
    assert "2026-06-25" in doc.title


def test_normalize_appointment_content_has_type() -> None:
    doc = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    assert "30 Minute Consultation" in doc.content


def test_normalize_appointment_content_has_client_name() -> None:
    doc = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    assert "Alice Smith" in doc.content


def test_normalize_appointment_content_has_email() -> None:
    doc = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    assert "alice@example.com" in doc.content


def test_normalize_appointment_content_has_date_and_time() -> None:
    doc = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    assert "2026-06-25" in doc.content
    assert "10:00am" in doc.content


def test_normalize_appointment_content_has_status() -> None:
    doc = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    assert "scheduled" in doc.content


def test_normalize_appointment_metadata_fields() -> None:
    doc = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["id"] == 1001
    assert meta["type"] == "30 Minute Consultation"
    assert meta["email"] == "alice@example.com"
    assert meta["date"] == "2026-06-25"
    assert meta["status"] == "scheduled"
    assert meta["calendar"] == "Main Calendar"


def test_normalize_appointment_connector_and_tenant_ids() -> None:
    doc = normalize_appointment(SAMPLE_APPOINTMENT, CONNECTOR_ID, TENANT_ID)
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_appointment_missing_phone_omits_phone_line() -> None:
    appt = {**SAMPLE_APPOINTMENT, "phone": ""}
    doc = normalize_appointment(appt, CONNECTOR_ID, TENANT_ID)
    assert "Phone:" not in doc.content


def test_normalize_appointment_missing_notes_omits_notes_line() -> None:
    appt = {**SAMPLE_APPOINTMENT, "notes": ""}
    doc = normalize_appointment(appt, CONNECTOR_ID, TENANT_ID)
    assert "Notes:" not in doc.content


# ── normalize_client ─────────────────────────────────────────────────────────


def test_normalize_client_source_id_is_16_chars() -> None:
    doc = normalize_client(SAMPLE_CLIENT, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_client_source_id_is_deterministic() -> None:
    doc1 = normalize_client(SAMPLE_CLIENT, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_client(SAMPLE_CLIENT, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_client_different_ids_differ() -> None:
    doc1 = normalize_client(SAMPLE_CLIENT, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_client(SAMPLE_CLIENT_2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_client_title() -> None:
    doc = normalize_client(SAMPLE_CLIENT, CONNECTOR_ID, TENANT_ID)
    assert "Alice Smith" in doc.title


def test_normalize_client_content_has_email() -> None:
    doc = normalize_client(SAMPLE_CLIENT, CONNECTOR_ID, TENANT_ID)
    assert "alice@example.com" in doc.content


def test_normalize_client_content_has_phone() -> None:
    doc = normalize_client(SAMPLE_CLIENT, CONNECTOR_ID, TENANT_ID)
    assert "+1-555-0100" in doc.content


def test_normalize_client_content_has_notes() -> None:
    doc = normalize_client(SAMPLE_CLIENT, CONNECTOR_ID, TENANT_ID)
    assert "VIP client" in doc.content


def test_normalize_client_metadata_fields() -> None:
    doc = normalize_client(SAMPLE_CLIENT, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["id"] == 5001
    assert meta["first_name"] == "Alice"
    assert meta["last_name"] == "Smith"
    assert meta["email"] == "alice@example.com"


def test_normalize_client_empty_name_falls_back_to_email() -> None:
    client = {**SAMPLE_CLIENT, "firstName": "", "lastName": ""}
    doc = normalize_client(client, CONNECTOR_ID, TENANT_ID)
    assert "alice@example.com" in doc.title


# ── normalize_appointment_type ────────────────────────────────────────────────


def test_normalize_appointment_type_source_id_is_16_chars() -> None:
    doc = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_appointment_type_source_id_is_deterministic() -> None:
    doc1 = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_appointment_type_different_ids_differ() -> None:
    doc1 = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE_2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_appointment_type_title() -> None:
    doc = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE, CONNECTOR_ID, TENANT_ID)
    assert "30 Minute Consultation" in doc.title


def test_normalize_appointment_type_content_has_duration() -> None:
    doc = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE, CONNECTOR_ID, TENANT_ID)
    assert "30 minutes" in doc.content


def test_normalize_appointment_type_content_has_category() -> None:
    doc = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE, CONNECTOR_ID, TENANT_ID)
    assert "Consulting" in doc.content


def test_normalize_appointment_type_content_has_description() -> None:
    doc = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE, CONNECTOR_ID, TENANT_ID)
    assert "free 30-minute intro call" in doc.content


def test_normalize_appointment_type_metadata_fields() -> None:
    doc = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["id"] == 101
    assert meta["name"] == "30 Minute Consultation"
    assert meta["duration"] == 30
    assert meta["active"] is True


def test_normalize_appointment_type_prefix_differs_from_appointment() -> None:
    """appointment_type:101 hash != appointment:101 hash."""
    at_doc = normalize_appointment_type(SAMPLE_APPOINTMENT_TYPE, CONNECTOR_ID, TENANT_ID)
    appt = {**SAMPLE_APPOINTMENT, "id": 101}
    appt_doc = normalize_appointment(appt, CONNECTOR_ID, TENANT_ID)
    assert at_doc.source_id != appt_doc.source_id


# ── with_retry ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    mock_fn = AsyncMock(
        side_effect=[AcuityNetworkError("fail"), AcuityNetworkError("fail"), {"ok": True}]
    )
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=AcuityAuthError("invalid creds", 401))
    with pytest.raises(AcuityAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=AcuityNetworkError("persistent failure"))
    with pytest.raises(AcuityNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=AcuityRateLimitError("429", retry_after=0))
    with pytest.raises(AcuityRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


# ── AcuityHTTPClient ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_get_me_uses_basic_auth() -> None:
    from client.http_client import AcuityHTTPClient

    client = AcuityHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    import aiohttp
    with patch.object(aiohttp, "ClientSession", return_value=mock_session):
        result = await client.get_me(USER_ID, API_KEY)

    assert result["name"] == "Shielva Demo Business"
    call_kwargs = mock_session.request.call_args
    auth_arg = call_kwargs.kwargs.get("auth")
    assert auth_arg is not None
    assert auth_arg.login == USER_ID


@pytest.mark.asyncio
async def test_http_client_get_appointments_passes_params() -> None:
    from client.http_client import AcuityHTTPClient

    client = AcuityHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=[SAMPLE_APPOINTMENT])
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    import aiohttp
    with patch.object(aiohttp, "ClientSession", return_value=mock_session):
        result = await client.get_appointments(
            USER_ID, API_KEY, page=2, max=10,
            min_date="2026-06-01", max_date="2026-06-30"
        )

    assert result == [SAMPLE_APPOINTMENT]
    call_kwargs = mock_session.request.call_args
    params = call_kwargs.kwargs.get("params", {})
    assert params["page"] == 2
    assert params["max"] == 10
    assert params["minDate"] == "2026-06-01"
    assert params["maxDate"] == "2026-06-30"


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_401() -> None:
    from client.http_client import AcuityHTTPClient

    client = AcuityHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.json = AsyncMock(return_value={"message": "Unauthorized"})
    mock_response.headers = {}
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    import aiohttp
    with patch.object(aiohttp, "ClientSession", return_value=mock_session):
        with pytest.raises(AcuityAuthError):
            await client.get_me(USER_ID, API_KEY)


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_403() -> None:
    from client.http_client import AcuityHTTPClient

    client = AcuityHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.json = AsyncMock(return_value={"message": "Forbidden"})
    mock_response.headers = {}
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    import aiohttp
    with patch.object(aiohttp, "ClientSession", return_value=mock_session):
        with pytest.raises(AcuityAuthError):
            await client.get_me(USER_ID, API_KEY)


@pytest.mark.asyncio
async def test_http_client_raises_not_found_on_404() -> None:
    from client.http_client import AcuityHTTPClient

    client = AcuityHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.json = AsyncMock(return_value={"message": "Not Found"})
    mock_response.headers = {}
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    import aiohttp
    with patch.object(aiohttp, "ClientSession", return_value=mock_session):
        with pytest.raises(AcuityNotFoundError):
            await client.get_me(USER_ID, API_KEY)


@pytest.mark.asyncio
async def test_http_client_raises_rate_limit_on_429() -> None:
    from client.http_client import AcuityHTTPClient

    client = AcuityHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.json = AsyncMock(return_value={"message": "Rate limited"})
    mock_response.headers = {"Retry-After": "60"}
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    import aiohttp
    with patch.object(aiohttp, "ClientSession", return_value=mock_session):
        with pytest.raises(AcuityRateLimitError) as exc_info:
            await client.get_me(USER_ID, API_KEY)
    assert exc_info.value.retry_after == 60.0


@pytest.mark.asyncio
async def test_http_client_raises_network_error_on_500() -> None:
    from client.http_client import AcuityHTTPClient

    client = AcuityHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.json = AsyncMock(return_value={"message": "Internal Server Error"})
    mock_response.headers = {}
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    import aiohttp
    with patch.object(aiohttp, "ClientSession", return_value=mock_session):
        with pytest.raises(AcuityNetworkError):
            await client.get_me(USER_ID, API_KEY)


# ── install() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success(connector: AcuitySchedulingConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
    connector._make_client = lambda: mock_client

    result = await connector.install()

    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Shielva Demo Business" in result.message


@pytest.mark.asyncio
async def test_install_missing_user_id() -> None:
    c = AcuitySchedulingConnector(
        tenant_id=TENANT_ID,
        config={"api_key": API_KEY},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "user_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = AcuitySchedulingConnector(
        tenant_id=TENANT_ID,
        config={"user_id": USER_ID},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_missing_both_credentials() -> None:
    c = AcuitySchedulingConnector(tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "user_id" in result.message
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector: AcuitySchedulingConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(side_effect=AcuityAuthError("Invalid credentials", 401))
    connector._make_client = lambda: mock_client

    result = await connector.install()

    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid credentials" in result.message


@pytest.mark.asyncio
async def test_install_network_error(connector: AcuitySchedulingConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(side_effect=AcuityNetworkError("Connection refused"))
    connector._make_client = lambda: mock_client

    result = await connector.install()

    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success(connector: AcuitySchedulingConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
    connector._make_client = lambda: mock_client

    assert connector._http_client is None
    await connector.install()
    assert connector._http_client is not None


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: AcuitySchedulingConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
    connector._make_client = lambda: mock_client

    result = await connector.health_check()

    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Shielva Demo Business" in result.message


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = AcuitySchedulingConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: AcuitySchedulingConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(side_effect=AcuityAuthError("Unauthorized", 401))
    connector._make_client = lambda: mock_client

    result = await connector.health_check()

    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: AcuitySchedulingConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(side_effect=AcuityNetworkError("Timeout"))
    connector._make_client = lambda: mock_client

    result = await connector.health_check()

    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_uses_email_when_no_name(connector: AcuitySchedulingConnector) -> None:
    response = {"id": 999, "name": "", "email": "owner@business.com"}
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(return_value=response)
    connector._make_client = lambda: mock_client

    result = await connector.health_check()

    assert result.health == ConnectorHealth.HEALTHY
    assert "owner@business.com" in result.message


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_returns_sync_result(connector_with_mock_client: AcuitySchedulingConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_appointments = AsyncMock(return_value=[SAMPLE_APPOINTMENT])
    c._http_client.get_clients = AsyncMock(return_value=[SAMPLE_CLIENT])
    c._http_client.get_appointment_types = AsyncMock(return_value=[SAMPLE_APPOINTMENT_TYPE])

    result = await c.sync()

    from models import SyncResult
    assert isinstance(result, SyncResult)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_counts_appointments_and_clients_and_types(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_appointments = AsyncMock(return_value=[SAMPLE_APPOINTMENT])
    c._http_client.get_clients = AsyncMock(return_value=[SAMPLE_CLIENT, SAMPLE_CLIENT_2])
    c._http_client.get_appointment_types = AsyncMock(
        return_value=[SAMPLE_APPOINTMENT_TYPE, SAMPLE_APPOINTMENT_TYPE_2]
    )

    result = await c.sync()

    assert result.documents_found == 5  # 1 appt + 2 clients + 2 types
    assert result.documents_synced == 5
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_empty_returns_completed(connector_with_mock_client: AcuitySchedulingConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_appointments = AsyncMock(return_value=[])
    c._http_client.get_clients = AsyncMock(return_value=[])
    c._http_client.get_appointment_types = AsyncMock(return_value=[])

    result = await c.sync()

    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_appointments_api_error_returns_failed(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_appointments = AsyncMock(side_effect=AcuityError("server error", 500))

    result = await c.sync()

    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_partial_when_normalization_fails(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    # Second appointment will raise during normalization
    bad_appt = {"id": None}  # id=None still works, but let's inject a direct failure
    c._http_client.get_appointments = AsyncMock(return_value=[SAMPLE_APPOINTMENT, bad_appt])
    c._http_client.get_clients = AsyncMock(return_value=[])
    c._http_client.get_appointment_types = AsyncMock(return_value=[])

    # Patch normalize to fail on the second call
    call_count = {"n": 0}
    orig_normalize = normalize_appointment

    def patched_normalize(appt: dict, *args, **kwargs):  # type: ignore[override]
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ValueError("normalization failed")
        return orig_normalize(appt, *args, **kwargs)

    with patch("connector.normalize_appointment", side_effect=patched_normalize):
        result = await c.sync()

    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1


@pytest.mark.asyncio
async def test_sync_with_date_filters_passes_to_client(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_appointments = AsyncMock(return_value=[])
    c._http_client.get_clients = AsyncMock(return_value=[])
    c._http_client.get_appointment_types = AsyncMock(return_value=[])

    await c.sync(min_date="2026-06-01", max_date="2026-06-30")

    call_kwargs = c._http_client.get_appointments.call_args
    assert call_kwargs.kwargs.get("min_date") == "2026-06-01"
    assert call_kwargs.kwargs.get("max_date") == "2026-06-30"


# ── list_appointments ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_appointments_returns_list(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_appointments = AsyncMock(return_value=[SAMPLE_APPOINTMENT])

    result = await c.list_appointments()

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == 1001


@pytest.mark.asyncio
async def test_list_appointments_passes_date_filters(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_appointments = AsyncMock(return_value=[])

    await c.list_appointments(min_date="2026-06-01", max_date="2026-06-30")

    call_kwargs = c._http_client.get_appointments.call_args
    assert call_kwargs.kwargs.get("min_date") == "2026-06-01"
    assert call_kwargs.kwargs.get("max_date") == "2026-06-30"


@pytest.mark.asyncio
async def test_list_appointments_empty_returns_empty_list(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_appointments = AsyncMock(return_value=[])

    result = await c.list_appointments()

    assert result == []


# ── list_clients ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_clients_returns_list(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_clients = AsyncMock(return_value=[SAMPLE_CLIENT, SAMPLE_CLIENT_2])

    result = await c.list_clients()

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_list_clients_empty_returns_empty_list(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_clients = AsyncMock(return_value=[])

    result = await c.list_clients()

    assert result == []


# ── list_appointment_types ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_appointment_types_returns_list(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_appointment_types = AsyncMock(
        return_value=[SAMPLE_APPOINTMENT_TYPE, SAMPLE_APPOINTMENT_TYPE_2]
    )

    result = await c.list_appointment_types()

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["name"] == "30 Minute Consultation"


@pytest.mark.asyncio
async def test_list_appointment_types_empty_returns_empty_list(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_appointment_types = AsyncMock(return_value=[])

    result = await c.list_appointment_types()

    assert result == []


# ── list_calendars ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_calendars_returns_list(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_calendars = AsyncMock(return_value=[SAMPLE_CALENDAR])

    result = await c.list_calendars()

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "Main Calendar"


@pytest.mark.asyncio
async def test_list_calendars_empty_returns_empty_list(
    connector_with_mock_client: AcuitySchedulingConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_calendars = AsyncMock(return_value=[])

    result = await c.list_calendars()

    assert result == []


# ── Connector config & lifecycle ─────────────────────────────────────────────


def test_connector_loads_user_id_and_api_key() -> None:
    c = AcuitySchedulingConnector(config={"user_id": USER_ID, "api_key": API_KEY})
    assert c._user_id == USER_ID
    assert c._api_key == API_KEY


def test_connector_empty_config_has_no_credentials() -> None:
    c = AcuitySchedulingConnector()
    assert c._user_id == ""
    assert c._api_key == ""


def test_connector_missing_credentials_list_both() -> None:
    c = AcuitySchedulingConnector()
    missing = c._missing_credentials()
    assert "user_id" in missing
    assert "api_key" in missing


def test_connector_no_missing_when_both_provided() -> None:
    c = AcuitySchedulingConnector(config={"user_id": USER_ID, "api_key": API_KEY})
    assert c._missing_credentials() == []


def test_connector_id_stored() -> None:
    c = AcuitySchedulingConnector(
        connector_id=CONNECTOR_ID,
        config={"user_id": USER_ID, "api_key": API_KEY},
    )
    assert c.connector_id == CONNECTOR_ID


def test_connector_tenant_id_stored() -> None:
    c = AcuitySchedulingConnector(
        tenant_id=TENANT_ID,
        config={"user_id": USER_ID, "api_key": API_KEY},
    )
    assert c.tenant_id == TENANT_ID


def test_ensure_client_creates_on_first_call(connector: AcuitySchedulingConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: AcuitySchedulingConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2


@pytest.mark.asyncio
async def test_connector_context_manager(connector: AcuitySchedulingConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(connector_with_mock_client: AcuitySchedulingConnector) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


# ── CONNECTOR_TYPE / AUTH_TYPE constants ──────────────────────────────────────


def test_connector_type_constant() -> None:
    assert AcuitySchedulingConnector.CONNECTOR_TYPE == "acuity_scheduling"


def test_auth_type_constant() -> None:
    assert AcuitySchedulingConnector.AUTH_TYPE == "api_key"
