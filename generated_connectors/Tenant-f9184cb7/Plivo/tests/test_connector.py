"""Unit tests for PlivoConnector — respx-mocked, zero real network I/O."""
from __future__ import annotations

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth
from connector import PlivoConnector
from exceptions import PlivoAuthError, PlivoError

from tests.conftest import (
    CONNECTOR_ID,
    TEST_AUTH_ID,
    TEST_AUTH_TOKEN,
    TEST_BASE_URL,
    TEST_CONFIG,
    expected_basic_auth,
)

ACCOUNT_URL = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/"
MESSAGE_URL = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Message/"
CALL_URL = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Call/"
NUMBER_URL = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Number/"
APPLICATION_URL = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Application/"
RECORDING_URL = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Recording/"
PRICING_URL = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Pricing/"
SUBACCOUNT_URL = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Subaccount/"
ENDPOINT_URL = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Endpoint/"
PHONENUMBER_URL = f"{TEST_BASE_URL}/PhoneNumber/"


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_auth_id(connector):
    connector.config.pop("auth_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_auth_token(connector):
    connector.config.pop("auth_token", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# health_check() / get_account() — verifies Basic-auth header shape
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_account_sends_basic_auth_header(connector):
    route = respx.get(ACCOUNT_URL).mock(
        return_value=httpx.Response(200, json={"auth_id": TEST_AUTH_ID, "name": "Test Account"})
    )
    result = await connector.get_account()

    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == expected_basic_auth()
    assert req.headers["Accept"] == "application/json"
    assert req.headers["Content-Type"] == "application/json"
    assert result["auth_id"] == TEST_AUTH_ID


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(ACCOUNT_URL).mock(return_value=httpx.Response(200, json={"auth_id": TEST_AUTH_ID}))
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error_401(connector):
    respx.get(ACCOUNT_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid credentials"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.FAILED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# send_sms() — verifies body shape
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_send_sms_body_shape(connector):
    route = respx.post(MESSAGE_URL).mock(
        return_value=httpx.Response(
            202,
            json={"message_uuid": ["abc-123"], "api_id": "req-1", "message": "message(s) queued"},
        )
    )
    result = await connector.send_sms(
        src="+14155550100",
        dst="+14155550101",
        text="Hello from Plivo",
    )

    assert route.called
    req = route.calls.last.request
    import json as _json
    body = _json.loads(req.content)
    assert body["src"] == "+14155550100"
    assert body["dst"] == "+14155550101"
    assert body["text"] == "Hello from Plivo"
    assert body["type"] == "sms"
    assert body["method"] == "POST"
    assert body["log"] is True
    assert body["trackable"] is False
    # Optional keys must not appear when caller did not set them.
    assert "url" not in body
    assert "message_uuid" not in body
    assert result["message_uuid"] == ["abc-123"]


# ═══════════════════════════════════════════════════════════════════════════
# list_messages() — verifies date-range filters land in the query string
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_messages_with_date_range(connector):
    route = respx.get(MESSAGE_URL).mock(
        return_value=httpx.Response(200, json={"api_id": "x", "objects": [], "meta": {"total_count": 0}})
    )
    await connector.list_messages(
        message_state="delivered",
        message_time__gte="2026-06-01",
        message_time__lte="2026-06-30",
        limit=50,
    )

    assert route.called
    qp = dict(route.calls.last.request.url.params)
    assert qp["message_state"] == "delivered"
    assert qp["message_time__gte"] == "2026-06-01"
    assert qp["message_time__lte"] == "2026-06-30"
    assert qp["limit"] == "50"
    # Unset filters must be absent.
    assert "from" not in qp
    assert "to" not in qp


# ═══════════════════════════════════════════════════════════════════════════
# get_message()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_message(connector):
    uuid = "msg-uuid-1"
    url = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Message/{uuid}/"
    respx.get(url).mock(return_value=httpx.Response(200, json={"message_uuid": uuid, "message_state": "delivered"}))
    result = await connector.get_message(uuid)
    assert result["message_uuid"] == uuid
    assert result["message_state"] == "delivered"


# ═══════════════════════════════════════════════════════════════════════════
# make_call() — verifies answer_url lands in body
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_make_call_body_shape(connector):
    route = respx.post(CALL_URL).mock(
        return_value=httpx.Response(
            201,
            json={"request_uuid": "req-uuid-1", "api_id": "api-1", "message": "call fired"},
        )
    )
    result = await connector.make_call(
        from_="+14155550100",
        to="+14155550200",
        answer_url="https://example.com/answer.xml",
    )

    assert route.called
    import json as _json
    body = _json.loads(route.calls.last.request.content)
    assert body["from"] == "+14155550100"
    assert body["to"] == "+14155550200"
    assert body["answer_url"] == "https://example.com/answer.xml"
    assert body["answer_method"] == "POST"
    assert body["time_limit"] == 14400
    assert body["machine_detection"] == "false"
    assert "hangup_url" not in body
    assert result["request_uuid"] == "req-uuid-1"


# ═══════════════════════════════════════════════════════════════════════════
# list_calls() — verifies direction filter is applied
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_calls_direction_filter(connector):
    route = respx.get(CALL_URL).mock(
        return_value=httpx.Response(200, json={"api_id": "x", "objects": [], "meta": {"total_count": 0}})
    )
    await connector.list_calls(call_direction="outbound", limit=10)

    assert route.called
    qp = dict(route.calls.last.request.url.params)
    assert qp["call_direction"] == "outbound"
    assert qp["limit"] == "10"


# ═══════════════════════════════════════════════════════════════════════════
# hangup_call()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_hangup_call(connector):
    uuid = "call-uuid-1"
    url = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Call/{uuid}/"
    respx.delete(url).mock(return_value=httpx.Response(204))
    result = await connector.hangup_call(uuid)
    # 204 means empty body — handler returns {}
    assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════════════
# list_numbers()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_numbers(connector):
    route = respx.get(NUMBER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "api_id": "n-1",
                "objects": [{"number": "+14155550100", "country": "US"}],
                "meta": {"total_count": 1},
            },
        )
    )
    result = await connector.list_numbers(type="local")
    assert route.called
    qp = dict(route.calls.last.request.url.params)
    assert qp["type"] == "local"
    assert result["objects"][0]["number"] == "+14155550100"


# ═══════════════════════════════════════════════════════════════════════════
# search_phone_numbers() — note this hits /PhoneNumber/ at the v1 root, not /Account/
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_search_phone_numbers(connector):
    route = respx.get(PHONENUMBER_URL).mock(
        return_value=httpx.Response(200, json={"api_id": "p-1", "objects": [{"number": "+14155550999"}]})
    )
    result = await connector.search_phone_numbers(country_iso="US", pattern="415555")

    assert route.called
    qp = dict(route.calls.last.request.url.params)
    assert qp["country_iso"] == "US"
    assert qp["pattern"] == "415555"
    assert qp["type"] == "local"
    assert result["objects"][0]["number"] == "+14155550999"


# ═══════════════════════════════════════════════════════════════════════════
# buy_phone_number()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_buy_phone_number(connector):
    number = "+14155550999"
    url = f"{TEST_BASE_URL}/PhoneNumber/{number}/"
    route = respx.post(url).mock(
        return_value=httpx.Response(201, json={"status": "fulfilled", "numbers": [{"number": number}]})
    )
    result = await connector.buy_phone_number(number)
    assert route.called
    assert result["status"] == "fulfilled"


# ═══════════════════════════════════════════════════════════════════════════
# create_application()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_create_application(connector):
    route = respx.post(APPLICATION_URL).mock(
        return_value=httpx.Response(
            201,
            json={"app_id": "app-1", "message": "created"},
        )
    )
    result = await connector.create_application(
        app_name="Shielva Voice App",
        answer_url="https://example.com/answer.xml",
        hangup_url="https://example.com/hangup",
    )
    assert route.called
    import json as _json
    body = _json.loads(route.calls.last.request.content)
    assert body["app_name"] == "Shielva Voice App"
    assert body["answer_url"] == "https://example.com/answer.xml"
    assert body["hangup_url"] == "https://example.com/hangup"
    assert body["answer_method"] == "POST"
    assert result["app_id"] == "app-1"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — HTTP client retries internally and eventually succeeds
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success(connector, monkeypatch):
    # Speed test up — replace sleep with a no-op.
    import asyncio as _asyncio
    import client.http_client as _http
    import helpers.utils as _utils

    async def _no_sleep(_d):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(_utils.asyncio, "sleep", _no_sleep)

    route = respx.get(ACCOUNT_URL).mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json={"auth_id": TEST_AUTH_ID, "name": "ok"}),
        ]
    )
    result = await connector.get_account()
    assert route.call_count == 3
    assert result["auth_id"] == TEST_AUTH_ID


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type():
    assert PlivoConnector.CONNECTOR_TYPE == "plivo"


def test_auth_type():
    assert PlivoConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(PlivoConnector, "REQUIRED_CONFIG_KEYS")
    assert "auth_id" in PlivoConnector.REQUIRED_CONFIG_KEYS
    assert "auth_token" in PlivoConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    """OCP status map must cover the documented Plivo HTTP failure modes."""
    assert hasattr(PlivoConnector, "_STATUS_MAP")
    assert 401 in PlivoConnector._STATUS_MAP
    assert 403 in PlivoConnector._STATUS_MAP
    assert 429 in PlivoConnector._STATUS_MAP


def test_multi_tenant_independent_instances():
    """Two tenants must produce independent connector instances."""
    from connector import PlivoConnector as _PC
    c1 = _PC(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = _PC(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# send_mms — convenience wrapper forces type=mms
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_send_mms_forces_type_mms(connector):
    route = respx.post(MESSAGE_URL).mock(
        return_value=httpx.Response(202, json={"message_uuid": ["mms-1"], "api_id": "req-mms"})
    )
    await connector.send_mms(
        src="+14155550100",
        dst="+14155550101",
        text="Hello MMS",
        url="https://example.com/img.png",
    )
    assert route.called
    import json as _json
    body = _json.loads(route.calls.last.request.content)
    assert body["type"] == "mms"
    assert body["url"] == "https://example.com/img.png"


# ═══════════════════════════════════════════════════════════════════════════
# Recordings
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_recordings_filters(connector):
    route = respx.get(RECORDING_URL).mock(
        return_value=httpx.Response(
            200,
            json={"api_id": "r-1", "objects": [{"recording_id": "rec-1"}], "meta": {"total_count": 1}},
        )
    )
    await connector.list_recordings(call_uuid="call-abc", limit=5)
    assert route.called
    qp = dict(route.calls.last.request.url.params)
    assert qp["call_uuid"] == "call-abc"
    assert qp["limit"] == "5"


@pytest.mark.asyncio
@respx.mock
async def test_get_recording(connector):
    rid = "rec-xyz"
    url = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Recording/{rid}/"
    respx.get(url).mock(
        return_value=httpx.Response(200, json={"recording_id": rid, "recording_url": "https://x/y.mp3"})
    )
    result = await connector.get_recording(rid)
    assert result["recording_id"] == rid


# ═══════════════════════════════════════════════════════════════════════════
# Numbers — single resource
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_number(connector):
    n = "+14155550100"
    url = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Number/{n}/"
    respx.get(url).mock(
        return_value=httpx.Response(200, json={"number": n, "country": "US"})
    )
    result = await connector.get_number(n)
    assert result["number"] == n


# ═══════════════════════════════════════════════════════════════════════════
# Applications — get single
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_application(connector):
    app_id = "app-42"
    url = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Application/{app_id}/"
    respx.get(url).mock(
        return_value=httpx.Response(200, json={"app_id": app_id, "app_name": "Voice"})
    )
    result = await connector.get_application(app_id)
    assert result["app_id"] == app_id


# ═══════════════════════════════════════════════════════════════════════════
# Pricing · Subaccounts · Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_pricing_country_iso(connector):
    route = respx.get(PRICING_URL).mock(
        return_value=httpx.Response(
            200,
            json={"country_iso": "US", "voice": {}, "message": {}, "api_id": "p-1"},
        )
    )
    result = await connector.get_pricing(country_iso="US")
    assert route.called
    qp = dict(route.calls.last.request.url.params)
    assert qp["country_iso"] == "US"
    assert result["country_iso"] == "US"


@pytest.mark.asyncio
@respx.mock
async def test_list_subaccounts(connector):
    route = respx.get(SUBACCOUNT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"api_id": "s-1", "objects": [{"auth_id": "SAxxxx"}], "meta": {"total_count": 1}},
        )
    )
    result = await connector.list_subaccounts(limit=10)
    assert route.called
    assert result["objects"][0]["auth_id"] == "SAxxxx"


@pytest.mark.asyncio
@respx.mock
async def test_list_endpoints(connector):
    route = respx.get(ENDPOINT_URL).mock(
        return_value=httpx.Response(
            200,
            json={"api_id": "e-1", "objects": [{"endpoint_id": "ep-1"}], "meta": {"total_count": 1}},
        )
    )
    result = await connector.list_endpoints()
    assert route.called
    assert result["objects"][0]["endpoint_id"] == "ep-1"


# ═══════════════════════════════════════════════════════════════════════════
# 404 → PlivoNotFound
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_call_404_raises_plivo_not_found(connector):
    from exceptions import PlivoNotFound
    uuid = "call-missing"
    url = f"{TEST_BASE_URL}/Account/{TEST_AUTH_ID}/Call/{uuid}/"
    respx.get(url).mock(return_value=httpx.Response(404, json={"error": "not found"}))
    with pytest.raises(PlivoNotFound):
        await connector.get_call(uuid)


# ═══════════════════════════════════════════════════════════════════════════
# Basic-auth header — verify base64 encoding shape
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_basic_auth_header_is_base64(connector):
    """Authorization header must be 'Basic ' + base64(auth_id:auth_token)."""
    import base64
    route = respx.get(ACCOUNT_URL).mock(
        return_value=httpx.Response(200, json={"auth_id": TEST_AUTH_ID})
    )
    await connector.get_account()
    sent = route.calls.last.request.headers["Authorization"]
    assert sent.startswith("Basic ")
    encoded = sent[len("Basic "):]
    decoded = base64.b64decode(encoded).decode("ascii")
    assert decoded == f"{TEST_AUTH_ID}:{TEST_AUTH_TOKEN}"


# ═══════════════════════════════════════════════════════════════════════════
# sync() — normalises messages + calls into NormalizedDocument
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_sync_normalises_messages_and_calls(connector, mocker):
    """sync() must call ingest_document for every message + call returned."""
    respx.get(MESSAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "api_id": "x",
                "objects": [
                    {
                        "message_uuid": "msg-1",
                        "from_number": "+14155550100",
                        "to_number": "+14155550101",
                        "message_direction": "outbound",
                        "message_state": "delivered",
                        "message_time": "2026-06-20T12:00:00Z",
                        "text": "hi",
                    }
                ],
                "meta": {"total_count": 1},
            },
        )
    )
    respx.get(CALL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "api_id": "y",
                "objects": [
                    {
                        "call_uuid": "call-1",
                        "from_number": "+14155550100",
                        "to_number": "+14155550200",
                        "call_direction": "outbound",
                        "call_duration": 42,
                        "end_time": "2026-06-20T12:01:00Z",
                    }
                ],
                "meta": {"total_count": 1},
            },
        )
    )
    # Replace mocked ingest_document with a tracking AsyncMock so we can assert calls.
    from unittest.mock import AsyncMock
    ingest = AsyncMock()
    mocker.patch.object(PlivoConnector, "ingest_document", new=ingest)

    result = await connector.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0
    assert ingest.call_count == 2

    # Verify NormalizedDocument id format: f"{tenant_id}_{source_id}"
    first_doc = ingest.call_args_list[0].args[0]
    assert first_doc.id == f"{connector.tenant_id}_msg-1"
    assert first_doc.source_id == "msg-1"
    second_doc = ingest.call_args_list[1].args[0]
    assert second_doc.id == f"{connector.tenant_id}_call-1"
