"""Unit tests for AircallConnector — respx-mocked, zero real HTTP."""
import base64
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import AircallConnector
from exceptions import (
    AircallAuthError,
    AircallBadRequestError,
    AircallConflictError,
    AircallNotFound,
    AircallNotFoundError,
    AircallRateLimitError,
    AircallServerError,
)

from tests.conftest import (
    AIRCALL_BASE,
    CONNECTOR_ID,
    SAMPLE_CALL,
    TENANT_ID,
    TEST_API_ID,
    TEST_API_TOKEN,
    TEST_CONFIG,
)

_EXPECTED_BASIC = "Basic " + base64.b64encode(
    f"{TEST_API_ID}:{TEST_API_TOKEN}".encode("utf-8")
).decode("ascii")


# ═══════════════════════════════════════════════════════════════════════════
# Identity / class attributes
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_attribute():
    assert AircallConnector.CONNECTOR_TYPE == "aircall"


def test_auth_type_attribute():
    assert AircallConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(AircallConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_id" in AircallConnector.REQUIRED_CONFIG_KEYS
    assert "api_token" in AircallConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(AircallConnector, "_STATUS_MAP")
    assert 401 in AircallConnector._STATUS_MAP
    assert 403 in AircallConnector._STATUS_MAP
    assert 429 in AircallConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_install_success(connector):
    route = respx.get(f"{AIRCALL_BASE}/ping").mock(
        return_value=httpx.Response(200, json={"ping": "pong"})
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID
    assert route.called
    sent = route.calls.last.request
    assert sent.headers.get("authorization") == _EXPECTED_BASIC


@respx.mock
@pytest.mark.asyncio
async def test_install_missing_api_id(connector):
    connector.config.pop("api_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_install_missing_api_token(connector):
    connector.config.pop("api_token", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@respx.mock
@pytest.mark.asyncio
async def test_install_auth_error(connector):
    respx.get(f"{AIRCALL_BASE}/ping").mock(
        return_value=httpx.Response(401, json={"error": "invalid_credentials"})
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{AIRCALL_BASE}/ping").mock(
        return_value=httpx.Response(200, json={"ping": "pong"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_offline_token_expired(connector, no_retry_sleep):
    respx.get(f"{AIRCALL_BASE}/ping").mock(
        return_value=httpx.Response(401, json={"error": "expired"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_invalid_credentials(connector, no_retry_sleep):
    respx.get(f"{AIRCALL_BASE}/ping").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_basic_base64(connector):
    """Aircall uses HTTP Basic — verify the header is `Basic base64(api_id:api_token)`."""
    route = respx.get(f"{AIRCALL_BASE}/users").mock(
        return_value=httpx.Response(200, json={"users": [], "meta": {}})
    )
    await connector.list_users(per_page=1)
    sent = route.calls.last.request
    assert sent.headers.get("authorization") == _EXPECTED_BASIC
    assert sent.headers.get("authorization", "").startswith("Basic ")


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_users_success(connector):
    payload = {"users": [{"id": 42, "name": "Alice"}, {"id": 43, "name": "Bob"}], "meta": {}}
    route = respx.get(f"{AIRCALL_BASE}/users").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_users(per_page=25, page=2)
    assert result["users"][0]["id"] == 42
    params = route.calls.last.request.url.params
    assert params["per_page"] == "25"
    assert params["page"] == "2"


@respx.mock
@pytest.mark.asyncio
async def test_get_user_success(connector):
    respx.get(f"{AIRCALL_BASE}/users/42").mock(
        return_value=httpx.Response(200, json={"user": {"id": 42, "name": "Alice"}})
    )
    result = await connector.get_user(42)
    assert result["user"]["id"] == 42


# ═══════════════════════════════════════════════════════════════════════════
# Numbers
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_numbers_success(connector):
    respx.get(f"{AIRCALL_BASE}/numbers").mock(
        return_value=httpx.Response(200, json={"numbers": [{"id": 13}], "meta": {}})
    )
    result = await connector.list_numbers()
    assert result["numbers"][0]["id"] == 13


@respx.mock
@pytest.mark.asyncio
async def test_get_number_success(connector):
    respx.get(f"{AIRCALL_BASE}/numbers/13").mock(
        return_value=httpx.Response(200, json={"number": {"id": 13, "digits": "+18005551111"}})
    )
    result = await connector.get_number(13)
    assert result["number"]["id"] == 13


# ═══════════════════════════════════════════════════════════════════════════
# Calls
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_calls_with_filters(connector):
    route = respx.get(f"{AIRCALL_BASE}/calls").mock(
        return_value=httpx.Response(200, json={"calls": [SAMPLE_CALL], "meta": {}})
    )
    result = await connector.list_calls(
        per_page=10,
        page=1,
        from_date="1700000000",
        to_date="1700100000",
        direction="outbound",
        user_id=42,
    )
    assert result["calls"][0]["id"] == 99001
    params = route.calls.last.request.url.params
    assert params["from"] == "1700000000"
    assert params["to"] == "1700100000"
    assert params["direction"] == "outbound"
    assert params["user_id"] == "42"


@pytest.mark.asyncio
async def test_list_calls_rejects_invalid_direction(connector):
    with pytest.raises(ValueError, match="direction must be"):
        await connector.list_calls(direction="sideways")


@respx.mock
@pytest.mark.asyncio
async def test_get_call_success(connector):
    respx.get(f"{AIRCALL_BASE}/calls/99001").mock(
        return_value=httpx.Response(200, json={"call": SAMPLE_CALL})
    )
    result = await connector.get_call(99001)
    assert result["call"]["id"] == 99001
    assert result["call"]["direction"] == "outbound"


@respx.mock
@pytest.mark.asyncio
async def test_get_call_not_found_raises(connector, no_retry_sleep):
    respx.get(f"{AIRCALL_BASE}/calls/4040").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    with pytest.raises(AircallNotFound):
        await connector.get_call(4040)


# ═══════════════════════════════════════════════════════════════════════════
# start_outbound_call / transfer_call / assign_call
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_start_outbound_call_success(connector):
    route = respx.post(f"{AIRCALL_BASE}/users/42/calls").mock(
        return_value=httpx.Response(200, json={"id": 5005})
    )
    result = await connector.start_outbound_call(
        user_id=42, number_id=13, to="+14155551234"
    )
    assert result["id"] == 5005
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["to"] == "+14155551234"
    assert body["number_id"] == 13


@pytest.mark.asyncio
async def test_start_outbound_call_rejects_bad_phone(connector):
    with pytest.raises(ValueError, match="not a valid phone"):
        await connector.start_outbound_call(user_id=42, number_id=13, to="notaphone")


@respx.mock
@pytest.mark.asyncio
async def test_transfer_call_success(connector):
    route = respx.post(f"{AIRCALL_BASE}/calls/99001/transfers").mock(
        return_value=httpx.Response(200, json={"id": 99001, "transferred_to": 43})
    )
    result = await connector.transfer_call(call_id=99001, user_id=43)
    assert result["transferred_to"] == 43
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["user_id"] == 43


@respx.mock
@pytest.mark.asyncio
async def test_assign_call_success(connector):
    respx.put(f"{AIRCALL_BASE}/calls/99001/assignment").mock(
        return_value=httpx.Response(200, json={"id": 99001, "assigned_to": 44})
    )
    result = await connector.assign_call(call_id=99001, user_id=44)
    assert result["assigned_to"] == 44


# ═══════════════════════════════════════════════════════════════════════════
# Contacts — CRUD
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_with_search(connector):
    route = respx.get(f"{AIRCALL_BASE}/contacts").mock(
        return_value=httpx.Response(
            200,
            json={"contacts": [{"id": 1, "first_name": "Bob"}], "meta": {}},
        )
    )
    result = await connector.list_contacts(per_page=20, page=1, search="bob")
    assert result["contacts"][0]["first_name"] == "Bob"
    params = route.calls.last.request.url.params
    assert params["search"] == "bob"
    assert params["per_page"] == "20"


@respx.mock
@pytest.mark.asyncio
async def test_get_contact_success(connector):
    respx.get(f"{AIRCALL_BASE}/contacts/7").mock(
        return_value=httpx.Response(200, json={"contact": {"id": 7, "first_name": "Bob"}})
    )
    result = await connector.get_contact(7)
    assert result["contact"]["id"] == 7


@respx.mock
@pytest.mark.asyncio
async def test_create_contact_success(connector):
    route = respx.post(f"{AIRCALL_BASE}/contacts").mock(
        return_value=httpx.Response(
            201,
            json={"contact": {"id": 7, "first_name": "Bob", "last_name": "Customer"}},
        )
    )
    result = await connector.create_contact(
        first_name="Bob",
        last_name="Customer",
        company_name="Acme",
        phone_numbers=[{"label": "Work", "value": "+14155550100"}],
        emails=[{"label": "Work", "value": "bob@acme.test"}],
    )
    assert result["contact"]["id"] == 7
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["company_name"] == "Acme"
    assert body["emails"][0]["value"] == "bob@acme.test"


@pytest.mark.asyncio
async def test_create_contact_requires_a_name(connector):
    with pytest.raises(ValueError, match="first_name or last_name"):
        await connector.create_contact(first_name="", last_name="")


@respx.mock
@pytest.mark.asyncio
async def test_update_contact_posts_partial_payload(connector):
    route = respx.post(f"{AIRCALL_BASE}/contacts/7").mock(
        return_value=httpx.Response(200, json={"contact": {"id": 7, "first_name": "Robert"}})
    )
    result = await connector.update_contact(contact_id=7, first_name="Robert")
    assert result["contact"]["first_name"] == "Robert"
    body = _json.loads(route.calls.last.request.content.decode())
    assert body == {"first_name": "Robert"}


@pytest.mark.asyncio
async def test_update_contact_requires_a_field(connector):
    with pytest.raises(ValueError, match="at least one field"):
        await connector.update_contact(contact_id=7)


@respx.mock
@pytest.mark.asyncio
async def test_delete_contact_success(connector):
    respx.delete(f"{AIRCALL_BASE}/contacts/7").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_contact(7)
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Tags / Teams
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_teams(connector):
    respx.get(f"{AIRCALL_BASE}/teams").mock(
        return_value=httpx.Response(200, json={"teams": [{"id": 1}], "meta": {}})
    )
    result = await connector.list_teams(per_page=20)
    assert result["teams"][0]["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_list_tags(connector):
    respx.get(f"{AIRCALL_BASE}/tags").mock(
        return_value=httpx.Response(200, json={"tags": [{"id": 1, "name": "VIP"}]})
    )
    result = await connector.list_tags()
    assert result["tags"][0]["name"] == "VIP"


# ═══════════════════════════════════════════════════════════════════════════
# Webhooks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_webhooks(connector):
    respx.get(f"{AIRCALL_BASE}/webhooks").mock(
        return_value=httpx.Response(
            200,
            json={"webhooks": [{"webhook_id": "wh_1", "url": "https://x.test/hook"}], "meta": {}},
        )
    )
    result = await connector.list_webhooks(per_page=10, page=1)
    assert result["webhooks"][0]["webhook_id"] == "wh_1"


@respx.mock
@pytest.mark.asyncio
async def test_create_webhook_success(connector):
    route = respx.post(f"{AIRCALL_BASE}/webhooks").mock(
        return_value=httpx.Response(
            201,
            json={"webhook": {"webhook_id": "wh_new", "url": "https://x.test/hook"}},
        )
    )
    result = await connector.create_webhook(
        url="https://x.test/hook", events=["call.created", "call.ended"]
    )
    assert result["webhook"]["webhook_id"] == "wh_new"
    body = _json.loads(route.calls.last.request.content.decode())
    assert body["url"] == "https://x.test/hook"
    assert body["events"] == ["call.created", "call.ended"]


@pytest.mark.asyncio
async def test_create_webhook_requires_url(connector):
    with pytest.raises(ValueError, match="non-empty url"):
        await connector.create_webhook(url="")


# ═══════════════════════════════════════════════════════════════════════════
# Error mapping — 400 / 409 surface as typed errors
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_400_raises_bad_request(connector, no_retry_sleep):
    respx.post(f"{AIRCALL_BASE}/contacts").mock(
        return_value=httpx.Response(400, json={"error": "bad payload"})
    )
    with pytest.raises(AircallBadRequestError):
        await connector.create_contact(first_name="Bob", last_name="Smith")


@respx.mock
@pytest.mark.asyncio
async def test_409_raises_conflict(connector, no_retry_sleep):
    respx.post(f"{AIRCALL_BASE}/contacts").mock(
        return_value=httpx.Response(409, json={"error": "duplicate"})
    )
    with pytest.raises(AircallConflictError):
        await connector.create_contact(first_name="Bob", last_name="Smith")


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    responses = [
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(200, json={"users": [], "meta": {}}),
    ]
    route = respx.get(f"{AIRCALL_BASE}/users").mock(side_effect=responses)
    result = await connector.list_users()
    assert result == {"users": [], "meta": {}}
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    responses = [
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(200, json={"users": [], "meta": {}}),
    ]
    route = respx.get(f"{AIRCALL_BASE}/users").mock(side_effect=responses)
    result = await connector.list_users()
    assert result == {"users": [], "meta": {}}
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_exhaustion_raises_rate_limit(connector, no_retry_sleep, monkeypatch):
    import client.http_client as hc
    monkeypatch.setattr(hc, "_RETRY_MAX_ATTEMPTS", 1)

    respx.get(f"{AIRCALL_BASE}/users").mock(
        return_value=httpx.Response(429, json={"error": "rate limited"})
    )
    with pytest.raises(AircallRateLimitError):
        await connector.list_users()


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_sync_ingests_calls(connector):
    """sync() pages /calls and feeds each call into ingest_document."""
    page1 = {"calls": [SAMPLE_CALL, {**SAMPLE_CALL, "id": 99002}], "meta": {}}
    respx.get(f"{AIRCALL_BASE}/calls").mock(
        return_value=httpx.Response(200, json=page1)
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_synced == 2
    assert result.documents_found == 2


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_different_tenants_independent_instances():
    c1 = AircallConnector(tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = AircallConnector(tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — NormalizedDocument id = f"{connector_id}_{source_id}"
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_call_doc_id_format():
    from helpers.normalizer import normalize_call

    doc = normalize_call(SAMPLE_CALL, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.source_id == "99001"
    assert doc.id == f"{CONNECTOR_ID}_99001"
    assert doc.metadata["direction"] == "outbound"
    assert doc.metadata["raw_digits"] == "+14155551234"
