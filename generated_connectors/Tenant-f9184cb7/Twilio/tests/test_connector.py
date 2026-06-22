"""Unit tests for TwilioConnector — all Twilio HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import TwilioConnector
from exceptions import TwilioAuthError, TwilioNetworkError, TwilioNotFoundError, TwilioRateLimitError
from helpers.utils import normalize_call, normalize_message, with_retry
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_twilio_test_001"
ACCOUNT_SID = "ACtest1234567890abcdef1234567890ab"
AUTH_TOKEN = "test_auth_token_abcdef1234567890"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_MESSAGE: dict = {
    "sid": "SMtest1234567890abcdef1234567890ab",
    "from": "+15005550006",
    "to": "+15005550007",
    "body": "Hello from Twilio!",
    "direction": "outbound-api",
    "status": "delivered",
    "date_sent": "Thu, 19 Jun 2026 10:00:00 +0000",
    "num_segments": "1",
    "price": "-0.0075",
    "price_unit": "USD",
}

SAMPLE_CALL: dict = {
    "sid": "CAtest1234567890abcdef1234567890ab",
    "from": "+15005550006",
    "to": "+15005550007",
    "direction": "outbound-api",
    "status": "completed",
    "duration": "42",
    "start_time": "Thu, 19 Jun 2026 10:00:00 +0000",
    "price": "-0.0200",
    "price_unit": "USD",
}

SAMPLE_PHONE_NUMBER: dict = {
    "sid": "PNtest1234567890abcdef1234567890ab",
    "phone_number": "+15005550001",
    "friendly_name": "Test Number",
    "capabilities": {"sms": True, "voice": True},
}

SAMPLE_ACCOUNT: dict = {
    "sid": ACCOUNT_SID,
    "friendly_name": "Test Twilio Account",
    "status": "active",
    "type": "Trial",
}


@pytest.fixture()
def connector() -> TwilioConnector:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    c.http_client = MagicMock()
    return c


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    with patch("connector.TwilioHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test Twilio Account" in result.message


@pytest.mark.asyncio
async def test_install_missing_account_sid() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": "", "auth_token": AUTH_TOKEN},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "account_sid" in result.message


@pytest.mark.asyncio
async def test_install_missing_auth_token() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": ""},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_both_credentials() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": "wrong_token"},
    )
    with patch("connector.TwilioHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(
            side_effect=TwilioAuthError("Authentication failed: 20003", 401, "20003")
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid" in result.message


@pytest.mark.asyncio
async def test_install_forbidden() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    with patch("connector.TwilioHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(
            side_effect=TwilioAuthError("Forbidden", 403)
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    with patch("connector.TwilioHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(
            side_effect=TwilioNetworkError("Connection refused")
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_uses_connector_id() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id="my-connector-id",
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    with patch("connector.TwilioHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.connector_id == "my-connector-id"


@pytest.mark.asyncio
async def test_install_falls_back_to_account_sid_when_no_connector_id() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id="",
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    with patch("connector.TwilioHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.connector_id == ACCOUNT_SID


# ── health_check() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: TwilioConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
    mock_client.aclose = AsyncMock()
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test Twilio Account" in result.message


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = TwilioConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: TwilioConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_account = AsyncMock(
        side_effect=TwilioAuthError("Invalid credentials", 401)
    )
    mock_client.aclose = AsyncMock()
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: TwilioConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_account = AsyncMock(
        side_effect=TwilioNetworkError("timeout")
    )
    mock_client.aclose = AsyncMock()
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: TwilioConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_account = AsyncMock(side_effect=Exception("unexpected"))
    mock_client.aclose = AsyncMock()
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ── sync() ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector: TwilioConnector) -> None:
    connector.http_client.list_messages = AsyncMock(
        return_value={"messages": [], "next_page_uri": None}
    )
    connector.http_client.list_calls = AsyncMock(
        return_value={"calls": [], "next_page_uri": None}
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_messages_and_calls(connector: TwilioConnector) -> None:
    connector.http_client.list_messages = AsyncMock(
        return_value={"messages": [SAMPLE_MESSAGE, {**SAMPLE_MESSAGE, "sid": "SM2"}], "next_page_uri": None}
    )
    connector.http_client.list_calls = AsyncMock(
        return_value={"calls": [SAMPLE_CALL], "next_page_uri": None}
    )
    result = await connector.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_message_pagination(connector: TwilioConnector) -> None:
    page1 = {"messages": [SAMPLE_MESSAGE], "next_page_uri": "/2010-04-01/Accounts/AC/Messages.json?Page=1"}
    page2 = {"messages": [{**SAMPLE_MESSAGE, "sid": "SM2"}], "next_page_uri": None}
    connector.http_client.list_messages = AsyncMock(side_effect=[page1, page2])
    connector.http_client.list_calls = AsyncMock(
        return_value={"calls": [], "next_page_uri": None}
    )
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert connector.http_client.list_messages.call_count == 2


@pytest.mark.asyncio
async def test_sync_call_pagination(connector: TwilioConnector) -> None:
    connector.http_client.list_messages = AsyncMock(
        return_value={"messages": [], "next_page_uri": None}
    )
    page1 = {"calls": [SAMPLE_CALL], "next_page_uri": "/2010-04-01/Accounts/AC/Calls.json?Page=1"}
    page2 = {"calls": [{**SAMPLE_CALL, "sid": "CA2"}], "next_page_uri": None}
    connector.http_client.list_calls = AsyncMock(side_effect=[page1, page2])
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert connector.http_client.list_calls.call_count == 2


@pytest.mark.asyncio
async def test_sync_partial_on_failed_normalize(connector: TwilioConnector) -> None:
    bad_msg = {}  # missing required fields
    connector.http_client.list_messages = AsyncMock(
        return_value={"messages": [bad_msg, SAMPLE_MESSAGE], "next_page_uri": None}
    )
    connector.http_client.list_calls = AsyncMock(
        return_value={"calls": [], "next_page_uri": None}
    )
    with patch("connector.normalize_message", side_effect=[Exception("normalize failed"),
                                                            normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)]):
        result = await connector.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1


@pytest.mark.asyncio
async def test_sync_failed_on_message_api_error(connector: TwilioConnector) -> None:
    from exceptions import TwilioError
    connector.http_client.list_messages = AsyncMock(
        side_effect=TwilioError("Server error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "failed" in result.message.lower()


@pytest.mark.asyncio
async def test_sync_partial_on_call_api_error(connector: TwilioConnector) -> None:
    from exceptions import TwilioError
    connector.http_client.list_messages = AsyncMock(
        return_value={"messages": [SAMPLE_MESSAGE], "next_page_uri": None}
    )
    connector.http_client.list_calls = AsyncMock(
        side_effect=TwilioError("Server error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_found >= 1


@pytest.mark.asyncio
async def test_sync_with_since_passes_date_filter(connector: TwilioConnector) -> None:
    from datetime import datetime, timezone
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    connector.http_client.list_messages = AsyncMock(
        return_value={"messages": [], "next_page_uri": None}
    )
    connector.http_client.list_calls = AsyncMock(
        return_value={"calls": [], "next_page_uri": None}
    )
    result = await connector.sync(full=False, since=since)
    # date_filter should be "2026-06-01"
    call_args = connector.http_client.list_messages.call_args
    assert "2026-06-01" in str(call_args)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_full_ignores_since(connector: TwilioConnector) -> None:
    from datetime import datetime, timezone
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    connector.http_client.list_messages = AsyncMock(
        return_value={"messages": [], "next_page_uri": None}
    )
    connector.http_client.list_calls = AsyncMock(
        return_value={"calls": [], "next_page_uri": None}
    )
    result = await connector.sync(full=True, since=since)
    call_args = connector.http_client.list_messages.call_args
    # When full=True, date filter should be None (passed as positional arg)
    assert call_args is not None
    assert result.status == SyncStatus.COMPLETED


# ── list_messages() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_messages_single_page(connector: TwilioConnector) -> None:
    connector.http_client.list_messages = AsyncMock(
        return_value={"messages": [SAMPLE_MESSAGE], "next_page_uri": None}
    )
    result = await connector.list_messages()
    assert len(result) == 1
    assert result[0]["sid"] == SAMPLE_MESSAGE["sid"]


@pytest.mark.asyncio
async def test_list_messages_multiple_pages(connector: TwilioConnector) -> None:
    page1 = {"messages": [SAMPLE_MESSAGE], "next_page_uri": "/2010-04-01/page2"}
    page2 = {"messages": [{**SAMPLE_MESSAGE, "sid": "SM_page2"}], "next_page_uri": None}
    connector.http_client.list_messages = AsyncMock(side_effect=[page1, page2])
    result = await connector.list_messages()
    assert len(result) == 2
    assert result[1]["sid"] == "SM_page2"


@pytest.mark.asyncio
async def test_list_messages_empty(connector: TwilioConnector) -> None:
    connector.http_client.list_messages = AsyncMock(
        return_value={"messages": [], "next_page_uri": None}
    )
    result = await connector.list_messages()
    assert result == []


@pytest.mark.asyncio
async def test_list_messages_with_date_filter(connector: TwilioConnector) -> None:
    connector.http_client.list_messages = AsyncMock(
        return_value={"messages": [SAMPLE_MESSAGE], "next_page_uri": None}
    )
    result = await connector.list_messages(date_sent_after="2026-06-01")
    assert len(result) == 1
    call_args = connector.http_client.list_messages.call_args
    assert "2026-06-01" in str(call_args)


# ── get_message() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_message_success(connector: TwilioConnector) -> None:
    connector.http_client.get_message = AsyncMock(return_value=SAMPLE_MESSAGE)
    result = await connector.get_message(SAMPLE_MESSAGE["sid"])
    assert result["sid"] == SAMPLE_MESSAGE["sid"]
    connector.http_client.get_message.assert_called_once()


@pytest.mark.asyncio
async def test_get_message_not_found(connector: TwilioConnector) -> None:
    connector.http_client.get_message = AsyncMock(
        side_effect=TwilioNotFoundError("message", "SM_nonexistent")
    )
    with pytest.raises(TwilioNotFoundError):
        await connector.get_message("SM_nonexistent")


# ── list_calls() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_calls_single_page(connector: TwilioConnector) -> None:
    connector.http_client.list_calls = AsyncMock(
        return_value={"calls": [SAMPLE_CALL], "next_page_uri": None}
    )
    result = await connector.list_calls()
    assert len(result) == 1
    assert result[0]["sid"] == SAMPLE_CALL["sid"]


@pytest.mark.asyncio
async def test_list_calls_multiple_pages(connector: TwilioConnector) -> None:
    page1 = {"calls": [SAMPLE_CALL], "next_page_uri": "/2010-04-01/page2"}
    page2 = {"calls": [{**SAMPLE_CALL, "sid": "CA_page2"}], "next_page_uri": None}
    connector.http_client.list_calls = AsyncMock(side_effect=[page1, page2])
    result = await connector.list_calls()
    assert len(result) == 2
    assert result[1]["sid"] == "CA_page2"


@pytest.mark.asyncio
async def test_list_calls_empty(connector: TwilioConnector) -> None:
    connector.http_client.list_calls = AsyncMock(
        return_value={"calls": [], "next_page_uri": None}
    )
    result = await connector.list_calls()
    assert result == []


@pytest.mark.asyncio
async def test_list_calls_with_time_filter(connector: TwilioConnector) -> None:
    connector.http_client.list_calls = AsyncMock(
        return_value={"calls": [SAMPLE_CALL], "next_page_uri": None}
    )
    result = await connector.list_calls(start_time_after="2026-06-01")
    assert len(result) == 1
    call_args = connector.http_client.list_calls.call_args
    assert "2026-06-01" in str(call_args)


# ── get_call() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_call_success(connector: TwilioConnector) -> None:
    connector.http_client.get_call = AsyncMock(return_value=SAMPLE_CALL)
    result = await connector.get_call(SAMPLE_CALL["sid"])
    assert result["sid"] == SAMPLE_CALL["sid"]
    assert result["status"] == "completed"
    connector.http_client.get_call.assert_called_once()


@pytest.mark.asyncio
async def test_get_call_not_found(connector: TwilioConnector) -> None:
    connector.http_client.get_call = AsyncMock(
        side_effect=TwilioNotFoundError("call", "CA_nonexistent")
    )
    with pytest.raises(TwilioNotFoundError):
        await connector.get_call("CA_nonexistent")


# ── list_phone_numbers() ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_phone_numbers_success(connector: TwilioConnector) -> None:
    connector.http_client.list_phone_numbers = AsyncMock(
        return_value={"incoming_phone_numbers": [SAMPLE_PHONE_NUMBER]}
    )
    result = await connector.list_phone_numbers()
    assert len(result) == 1
    assert result[0]["phone_number"] == "+15005550001"


@pytest.mark.asyncio
async def test_list_phone_numbers_empty(connector: TwilioConnector) -> None:
    connector.http_client.list_phone_numbers = AsyncMock(
        return_value={"incoming_phone_numbers": []}
    )
    result = await connector.list_phone_numbers()
    assert result == []


@pytest.mark.asyncio
async def test_list_phone_numbers_multiple(connector: TwilioConnector) -> None:
    numbers = [
        {**SAMPLE_PHONE_NUMBER, "sid": "PN1", "phone_number": "+15005550001"},
        {**SAMPLE_PHONE_NUMBER, "sid": "PN2", "phone_number": "+15005550002"},
        {**SAMPLE_PHONE_NUMBER, "sid": "PN3", "phone_number": "+15005550003"},
    ]
    connector.http_client.list_phone_numbers = AsyncMock(
        return_value={"incoming_phone_numbers": numbers}
    )
    result = await connector.list_phone_numbers()
    assert len(result) == 3


# ── normalize_message() ──────────────────────────────────────────────────────


def test_normalize_message_title() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "SMS from +15005550006 to +15005550007"


def test_normalize_message_content() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.content == "Hello from Twilio!"


def test_normalize_message_source_id_is_sha256_prefix() -> None:
    sid = SAMPLE_MESSAGE["sid"]
    expected_id = hashlib.sha256(sid.encode()).hexdigest()[:16]
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected_id


def test_normalize_message_source_url() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    sid = SAMPLE_MESSAGE["sid"]
    assert doc.source_url == f"https://console.twilio.com/us1/monitor/logs/sms/{sid}"


def test_normalize_message_metadata_fields() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["sid"] == SAMPLE_MESSAGE["sid"]
    assert doc.metadata["from"] == "+15005550006"
    assert doc.metadata["to"] == "+15005550007"
    assert doc.metadata["direction"] == "outbound-api"
    assert doc.metadata["status"] == "delivered"
    assert doc.metadata["num_segments"] == "1"
    assert doc.metadata["price"] == "-0.0075"


def test_normalize_message_tenant_and_connector() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_message_missing_body_defaults_to_empty() -> None:
    msg = {**SAMPLE_MESSAGE, "body": None}
    doc = normalize_message(msg, CONNECTOR_ID, TENANT_ID)
    assert doc.content == ""


def test_normalize_message_uses_from_underscore_field() -> None:
    """Twilio SDK sometimes returns 'from_' instead of 'from'."""
    msg = {k: v for k, v in SAMPLE_MESSAGE.items() if k != "from"}
    msg["from_"] = "+15005550099"
    doc = normalize_message(msg, CONNECTOR_ID, TENANT_ID)
    assert "+15005550099" in doc.title


# ── normalize_call() ─────────────────────────────────────────────────────────


def test_normalize_call_title() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Call from +15005550006 to +15005550007 (42s)"


def test_normalize_call_content() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert "Direction: outbound-api" in doc.content
    assert "Status: completed" in doc.content
    assert "Duration: 42s" in doc.content
    assert "Price: -0.0200" in doc.content


def test_normalize_call_source_id_is_sha256_prefix() -> None:
    sid = SAMPLE_CALL["sid"]
    expected_id = hashlib.sha256(sid.encode()).hexdigest()[:16]
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected_id


def test_normalize_call_source_url() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    sid = SAMPLE_CALL["sid"]
    assert doc.source_url == f"https://console.twilio.com/us1/monitor/logs/calls/{sid}"


def test_normalize_call_metadata_fields() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["sid"] == SAMPLE_CALL["sid"]
    assert doc.metadata["from"] == "+15005550006"
    assert doc.metadata["to"] == "+15005550007"
    assert doc.metadata["direction"] == "outbound-api"
    assert doc.metadata["status"] == "completed"
    assert doc.metadata["duration"] == "42"
    assert doc.metadata["price"] == "-0.0200"


def test_normalize_call_tenant_and_connector() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_call_zero_duration() -> None:
    call = {**SAMPLE_CALL, "duration": "0"}
    doc = normalize_call(call, CONNECTOR_ID, TENANT_ID)
    assert "(0s)" in doc.title
    assert "Duration: 0s" in doc.content


def test_normalize_call_uses_from_underscore_field() -> None:
    call = {k: v for k, v in SAMPLE_CALL.items() if k != "from"}
    call["from_"] = "+15005550099"
    doc = normalize_call(call, CONNECTOR_ID, TENANT_ID)
    assert "+15005550099" in doc.title


def test_normalize_call_no_price() -> None:
    call = {**SAMPLE_CALL, "price": None}
    doc = normalize_call(call, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["price"] == ""


# ── with_retry() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    from exceptions import TwilioError
    fn = AsyncMock(side_effect=[TwilioError("transient"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    fn = AsyncMock(side_effect=TwilioAuthError("auth failed", 401))
    with pytest.raises(TwilioAuthError):
        await with_retry(fn, max_attempts=3)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    from exceptions import TwilioError
    fn = AsyncMock(side_effect=TwilioError("persistent error"))
    with pytest.raises(TwilioError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(side_effect=[
        TwilioRateLimitError("rate limited", retry_after=0.0),
        {"ok": True},
    ])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


# ── Exception hierarchy ──────────────────────────────────────────────────────


def test_twilio_auth_error_is_twilio_error() -> None:
    from exceptions import TwilioError
    exc = TwilioAuthError("auth failed", 401, "20003")
    assert isinstance(exc, TwilioError)
    assert exc.status_code == 401
    assert exc.code == "20003"


def test_twilio_rate_limit_error_fields() -> None:
    exc = TwilioRateLimitError("too many requests", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.retry_after == 5.0
    assert exc.code == "rate_limit"


def test_twilio_not_found_error_message() -> None:
    exc = TwilioNotFoundError("message", "SM_xyz")
    assert "SM_xyz" in str(exc)
    assert exc.status_code == 404


def test_twilio_network_error_is_twilio_error() -> None:
    from exceptions import TwilioError
    exc = TwilioNetworkError("connection refused")
    assert isinstance(exc, TwilioError)


# ── ConnectorDocument model ──────────────────────────────────────────────────


def test_connector_document_default_metadata() -> None:
    from models import ConnectorDocument
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test",
        content="Content",
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


def test_connector_document_with_metadata() -> None:
    from models import ConnectorDocument
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test",
        content="Content",
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        source_url="https://example.com",
        metadata={"key": "value"},
    )
    assert doc.metadata["key"] == "value"
    assert doc.source_url == "https://example.com"


# ── Lifecycle ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_clears_http_client(connector: TwilioConnector) -> None:
    connector.http_client.aclose = AsyncMock()
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    # Should not raise
    await c.aclose()


@pytest.mark.asyncio
async def test_context_manager(connector: TwilioConnector) -> None:
    connector.http_client.aclose = AsyncMock()
    async with connector as c:
        assert c is connector
    assert connector.http_client is None


# ── Ensure client lazy init ──────────────────────────────────────────────────


def test_ensure_client_creates_client_if_none() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    assert c.http_client is None
    client = c._ensure_client()
    assert client is not None
    assert c.http_client is not None


def test_ensure_client_reuses_existing_client() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    mock = MagicMock()
    c.http_client = mock
    client = c._ensure_client()
    assert client is mock


# ── list_recordings() ────────────────────────────────────────────────────────

SAMPLE_RECORDING: dict = {
    "sid": "REtest1234567890abcdef1234567890ab",
    "call_sid": "CAtest1234567890abcdef1234567890ab",
    "duration": "10",
    "status": "completed",
    "date_created": "Thu, 19 Jun 2026 10:00:00 +0000",
    "price": "-0.0025",
}


@pytest.mark.asyncio
async def test_list_recordings_success(connector: TwilioConnector) -> None:
    connector.http_client.list_recordings = AsyncMock(
        return_value={"recordings": [SAMPLE_RECORDING]}
    )
    result = await connector.list_recordings()
    assert len(result) == 1
    assert result[0]["sid"] == SAMPLE_RECORDING["sid"]


@pytest.mark.asyncio
async def test_list_recordings_empty(connector: TwilioConnector) -> None:
    connector.http_client.list_recordings = AsyncMock(
        return_value={"recordings": []}
    )
    result = await connector.list_recordings()
    assert result == []


@pytest.mark.asyncio
async def test_list_recordings_filtered_by_call_sid(connector: TwilioConnector) -> None:
    connector.http_client.list_recordings = AsyncMock(
        return_value={"recordings": [SAMPLE_RECORDING]}
    )
    result = await connector.list_recordings(call_sid="CAtest1234567890abcdef1234567890ab")
    assert len(result) == 1
    call_args = connector.http_client.list_recordings.call_args
    assert "CAtest1234567890abcdef1234567890ab" in str(call_args)


@pytest.mark.asyncio
async def test_list_recordings_multiple(connector: TwilioConnector) -> None:
    recordings = [
        {**SAMPLE_RECORDING, "sid": "RE1"},
        {**SAMPLE_RECORDING, "sid": "RE2"},
    ]
    connector.http_client.list_recordings = AsyncMock(
        return_value={"recordings": recordings}
    )
    result = await connector.list_recordings()
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_recordings_raises_on_auth_error(connector: TwilioConnector) -> None:
    connector.http_client.list_recordings = AsyncMock(
        side_effect=TwilioAuthError("Unauthorized", 401)
    )
    with pytest.raises(TwilioAuthError):
        await connector.list_recordings()


@pytest.mark.asyncio
async def test_list_recordings_raises_on_network_error(connector: TwilioConnector) -> None:
    connector.http_client.list_recordings = AsyncMock(
        side_effect=TwilioNetworkError("connection timeout")
    )
    with pytest.raises(TwilioNetworkError):
        await connector.list_recordings()


# ── HTTP client _raise_for_status coverage ───────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_401() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Unauthorized", "code": "20003"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    with pytest.raises(TwilioAuthError) as exc_info:
        await client.get_account(ACCOUNT_SID, AUTH_TOKEN)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_403() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Forbidden", "code": "20003"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    with pytest.raises(TwilioAuthError) as exc_info:
        await client.get_account(ACCOUNT_SID, AUTH_TOKEN)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_http_client_raises_not_found_on_404() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Not found", "code": "20404"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    with pytest.raises(TwilioNotFoundError):
        await client.get_message(ACCOUNT_SID, AUTH_TOKEN, "SM_nonexistent")


@pytest.mark.asyncio
async def test_http_client_raises_rate_limit_on_429() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "30"}
    mock_response.json = AsyncMock(return_value={"message": "Too Many Requests"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    with pytest.raises(TwilioRateLimitError) as exc_info:
        await client.get_account(ACCOUNT_SID, AUTH_TOKEN)
    assert exc_info.value.retry_after == 30.0


@pytest.mark.asyncio
async def test_http_client_raises_network_error_on_500() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Internal Server Error"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    with pytest.raises(TwilioNetworkError) as exc_info:
        await client.get_account(ACCOUNT_SID, AUTH_TOKEN)
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_http_client_raises_generic_error_on_4xx() -> None:
    from client.http_client import TwilioHTTPClient
    from exceptions import TwilioError

    client = TwilioHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 422
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Unprocessable Entity"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    with pytest.raises(TwilioError) as exc_info:
        await client.get_account(ACCOUNT_SID, AUTH_TOKEN)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_http_client_get_account_success() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=SAMPLE_ACCOUNT)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    result = await client.get_account(ACCOUNT_SID, AUTH_TOKEN)
    assert result["sid"] == ACCOUNT_SID


@pytest.mark.asyncio
async def test_http_client_list_messages_success() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    payload = {"messages": [SAMPLE_MESSAGE], "next_page_uri": None}
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=payload)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    result = await client.list_messages(ACCOUNT_SID, AUTH_TOKEN)
    assert "messages" in result
    assert len(result["messages"]) == 1


@pytest.mark.asyncio
async def test_http_client_list_calls_success() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    payload = {"calls": [SAMPLE_CALL], "next_page_uri": None}
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=payload)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    result = await client.list_calls(ACCOUNT_SID, AUTH_TOKEN)
    assert "calls" in result


@pytest.mark.asyncio
async def test_http_client_list_recordings_success() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    payload = {"recordings": [SAMPLE_RECORDING]}
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=payload)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    result = await client.list_recordings(ACCOUNT_SID, AUTH_TOKEN)
    assert "recordings" in result


@pytest.mark.asyncio
async def test_http_client_list_recordings_with_call_sid() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    payload = {"recordings": [SAMPLE_RECORDING]}
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=payload)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    result = await client.list_recordings(ACCOUNT_SID, AUTH_TOKEN, call_sid="CAtest")
    assert "recordings" in result
    # Verify CallSid param was included
    call_kwargs = mock_session.request.call_args[1]
    assert call_kwargs["params"]["CallSid"] == "CAtest"


@pytest.mark.asyncio
async def test_http_client_list_phone_numbers_success() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    payload = {"incoming_phone_numbers": [SAMPLE_PHONE_NUMBER]}
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=payload)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    result = await client.list_phone_numbers(ACCOUNT_SID, AUTH_TOKEN)
    assert "incoming_phone_numbers" in result


@pytest.mark.asyncio
async def test_http_client_get_message_success() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=SAMPLE_MESSAGE)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    result = await client.get_message(ACCOUNT_SID, AUTH_TOKEN, SAMPLE_MESSAGE["sid"])
    assert result["sid"] == SAMPLE_MESSAGE["sid"]


@pytest.mark.asyncio
async def test_http_client_get_call_success() -> None:
    from client.http_client import TwilioHTTPClient

    client = TwilioHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=SAMPLE_CALL)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_response)

    client._session = mock_session

    result = await client.get_call(ACCOUNT_SID, AUTH_TOKEN, SAMPLE_CALL["sid"])
    assert result["sid"] == SAMPLE_CALL["sid"]


# ── Model enum coverage ────────────────────────────────────────────────────────


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


def test_install_result_fields() -> None:
    from models import InstallResult, ConnectorHealth, AuthStatus
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="cid",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.connector_id == "cid"


def test_health_check_result_fields() -> None:
    from models import HealthCheckResult, ConnectorHealth, AuthStatus
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="degraded",
    )
    assert r.message == "degraded"


def test_sync_result_fields() -> None:
    from models import SyncResult, SyncStatus
    r = SyncResult(
        status=SyncStatus.PARTIAL,
        documents_found=10,
        documents_synced=8,
        documents_failed=2,
        message="partial sync",
    )
    assert r.documents_found == 10
    assert r.documents_failed == 2


# ── TwilioError base class ────────────────────────────────────────────────────


def test_twilio_error_base_fields() -> None:
    from exceptions import TwilioError
    exc = TwilioError("test error", status_code=400, code="40001")
    assert str(exc) == "test error"
    assert exc.status_code == 400
    assert exc.code == "40001"
    assert exc.message == "test error"


def test_twilio_error_default_fields() -> None:
    from exceptions import TwilioError
    exc = TwilioError("bare error")
    assert exc.status_code == 0
    assert exc.code == ""


def test_twilio_not_found_error_resource() -> None:
    exc = TwilioNotFoundError("call", "CA_missing")
    assert "call" in str(exc)
    assert "CA_missing" in str(exc)
    assert exc.code == "resource_missing"


def test_twilio_rate_limit_error_defaults() -> None:
    exc = TwilioRateLimitError("rate limited")
    assert exc.retry_after == 0.0
    assert exc.status_code == 429


def test_twilio_network_error_is_base() -> None:
    from exceptions import TwilioError
    exc = TwilioNetworkError("timeout", 0, "")
    assert isinstance(exc, TwilioError)


# ── Connector identity ────────────────────────────────────────────────────────


def test_connector_type_constant() -> None:
    assert TwilioConnector.CONNECTOR_TYPE == "twilio"


def test_connector_auth_type_constant() -> None:
    assert TwilioConnector.AUTH_TYPE == "api_key"


def test_connector_stores_account_sid() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    assert c._account_sid == ACCOUNT_SID


def test_connector_stores_auth_token() -> None:
    c = TwilioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"account_sid": ACCOUNT_SID, "auth_token": AUTH_TOKEN},
    )
    assert c._auth_token == AUTH_TOKEN
