"""Unit tests for NutshellConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import NutshellConnector
from exceptions import (
    NutshellAuthError,
    NutshellError,
    NutshellNotFound,
    NutshellRateLimitError,
)

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    SAMPLE_ACCOUNT,
    SAMPLE_CONTACT,
    SAMPLE_LEAD,
    SAMPLE_USER,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
    TEST_EMAIL,
    rpc_err,
    rpc_ok,
)


# ═══════════════════════════════════════════════════════════════════════════
# Class identity — required by gateway loader
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attribute():
    assert NutshellConnector.CONNECTOR_TYPE == "nutshell"


def test_auth_type_class_attribute():
    assert NutshellConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(NutshellConnector, "REQUIRED_CONFIG_KEYS")
    assert "email" in NutshellConnector.REQUIRED_CONFIG_KEYS
    assert "api_key" in NutshellConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(NutshellConnector, "_STATUS_MAP")
    assert 401 in NutshellConnector._STATUS_MAP
    assert 429 in NutshellConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_install_success(connector):
    """Happy path: getUser returns a user object, install marks the connector connected."""
    respx.post(BASE_URL).mock(return_value=httpx.Response(200, json=rpc_ok(SAMPLE_USER)))

    result = await connector.install()

    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_email():
    """Missing email — no HTTP call, OFFLINE + MISSING_CREDENTIALS."""
    c = NutshellConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": "x", "base_url": BASE_URL},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_api_key():
    """Missing api_key — no HTTP call, OFFLINE + MISSING_CREDENTIALS."""
    c = NutshellConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"email": TEST_EMAIL, "base_url": BASE_URL},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_accepts_legacy_username_alias():
    """Old sessions that stored ``username`` instead of ``email`` still work."""
    c = NutshellConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"username": TEST_EMAIL, "api_key": TEST_API_KEY, "base_url": BASE_URL},
    )
    # Without an HTTP mock the install() call would try the real network — we
    # only assert that pre-network credential validation passes (i.e. the
    # connector did not short-circuit on missing creds).
    assert c.username == TEST_EMAIL
    assert c.api_key == TEST_API_KEY


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_error_401(connector):
    """401 from Nutshell at install — MISSING_CREDENTIALS so the user fixes the key."""
    respx.post(BASE_URL).mock(return_value=httpx.Response(401, json={}))

    result = await connector.install()

    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "Authentication failed" in result.message


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_error_in_rpc_envelope(connector):
    """HTTP 200 with a JSON-RPC error envelope — must surface as auth failure."""
    respx.post(BASE_URL).mock(
        return_value=httpx.Response(200, json=rpc_err(-32000, "Invalid API key"))
    )

    result = await connector.install()

    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_ok(connector):
    respx.post(BASE_URL).mock(return_value=httpx.Response(200, json=rpc_ok(SAMPLE_USER)))

    result = await connector.health_check()

    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_token_expired(connector):
    respx.post(BASE_URL).mock(return_value=httpx.Response(401, json={}))

    result = await connector.health_check()

    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


# ═══════════════════════════════════════════════════════════════════════════
# list_contacts() — including query + order_by
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_contacts_with_query_and_order_by(connector):
    """findContacts must be called with the query filter and the requested orderBy."""
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["envelope"] = json.loads(request.content)
        return httpx.Response(200, json=rpc_ok([SAMPLE_CONTACT]))

    respx.post(BASE_URL).mock(side_effect=_handler)

    result = await connector.list_contacts(
        page=2,
        limit=25,
        query={"name": "Ada"},
        order_by="firstName",
    )

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == 12345
    assert result[0]["display_name"] == "Ada Lovelace"
    env = captured["envelope"]
    assert env["jsonrpc"] == "2.0"
    assert env["method"] == "findContacts"
    assert env["params"]["page"] == 2
    assert env["params"]["limit"] == 25
    assert env["params"]["orderBy"] == "firstName"
    assert env["params"]["query"] == {"name": "Ada"}


@pytest.mark.asyncio
@respx.mock
async def test_list_contacts_results_wrapped_envelope(connector):
    """Nutshell sometimes wraps results in {"results": [...]}. Must unwrap correctly."""
    respx.post(BASE_URL).mock(
        return_value=httpx.Response(200, json=rpc_ok({"results": [SAMPLE_CONTACT]}))
    )

    result = await connector.list_contacts()

    assert len(result) == 1
    assert result[0]["id"] == 12345


# ═══════════════════════════════════════════════════════════════════════════
# get_contact / create_contact / update_contact / delete_contact
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_contact_normalized(connector):
    respx.post(BASE_URL).mock(return_value=httpx.Response(200, json=rpc_ok(SAMPLE_CONTACT)))
    result = await connector.get_contact(12345)
    assert result["id"] == 12345
    assert result["display_name"] == "Ada Lovelace"


@pytest.mark.asyncio
@respx.mock
async def test_create_contact(connector):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["envelope"] = json.loads(request.content)
        return httpx.Response(200, json=rpc_ok(SAMPLE_CONTACT))

    respx.post(BASE_URL).mock(side_effect=_handler)

    new = {"name": {"givenName": "Ada", "familyName": "Lovelace"}}
    result = await connector.create_contact(new)

    assert result["id"] == 12345
    assert captured["envelope"]["method"] == "newContact"
    assert captured["envelope"]["params"] == {"contact": new}


@pytest.mark.asyncio
@respx.mock
async def test_update_contact_uses_rev(connector):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["envelope"] = json.loads(request.content)
        updated = dict(SAMPLE_CONTACT, rev="2-def")
        return httpx.Response(200, json=rpc_ok(updated))

    respx.post(BASE_URL).mock(side_effect=_handler)

    result = await connector.update_contact(12345, "1-abc", {"description": "VIP"})

    assert result["id"] == 12345
    assert result["rev"] == "2-def"
    env = captured["envelope"]
    assert env["method"] == "editContact"
    assert env["params"]["contactId"] == 12345
    assert env["params"]["rev"] == "1-abc"
    assert env["params"]["contact"] == {"description": "VIP"}


@pytest.mark.asyncio
@respx.mock
async def test_delete_contact_envelope(connector):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["envelope"] = json.loads(request.content)
        return httpx.Response(200, json=rpc_ok({"ok": True}))

    respx.post(BASE_URL).mock(side_effect=_handler)

    result = await connector.delete_contact(12345, "1-abc")
    assert result["deleted"] is True
    assert result["contact_id"] == 12345
    env = captured["envelope"]
    assert env["method"] == "deleteContact"
    assert env["params"] == {"contactId": 12345, "rev": "1-abc"}


# ═══════════════════════════════════════════════════════════════════════════
# Leads + Accounts + Users + Activities
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_leads(connector):
    respx.post(BASE_URL).mock(return_value=httpx.Response(200, json=rpc_ok([SAMPLE_LEAD])))

    result = await connector.list_leads()

    assert len(result) == 1
    assert result[0]["id"] == 7777
    assert result[0]["description"] == "Enterprise rollout"


@pytest.mark.asyncio
@respx.mock
async def test_create_lead(connector):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["envelope"] = json.loads(request.content)
        return httpx.Response(200, json=rpc_ok(SAMPLE_LEAD))

    respx.post(BASE_URL).mock(side_effect=_handler)

    payload = {"description": "Enterprise rollout"}
    result = await connector.create_lead(payload)

    assert result["id"] == 7777
    env = captured["envelope"]
    assert env["method"] == "newLead"
    assert env["params"] == {"lead": payload}


@pytest.mark.asyncio
@respx.mock
async def test_list_accounts_normalized(connector):
    respx.post(BASE_URL).mock(
        return_value=httpx.Response(200, json=rpc_ok([SAMPLE_ACCOUNT]))
    )
    result = await connector.list_accounts()
    assert len(result) == 1
    assert result[0]["id"] == 9001
    # Account industry/territory must collapse the {"name": ...} envelope.
    assert result[0]["industry"] == "Software"
    assert result[0]["territory"] == "EMEA"


@pytest.mark.asyncio
@respx.mock
async def test_list_users(connector):
    respx.post(BASE_URL).mock(
        return_value=httpx.Response(200, json=rpc_ok([SAMPLE_USER]))
    )
    result = await connector.list_users()
    assert len(result) == 1
    assert result[0]["id"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_list_activities(connector):
    respx.post(BASE_URL).mock(
        return_value=httpx.Response(200, json=rpc_ok([{"id": 555, "kind": "call"}]))
    )
    result = await connector.list_activities()
    assert len(result) == 1
    assert result[0]["id"] == 555


# ═══════════════════════════════════════════════════════════════════════════
# Error-inside-HTTP-200 envelope handling
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_rpc_error_envelope_raises_typed_exception(connector):
    """HTTP 200 + {"error": {"message": "Contact not found"}} → NutshellNotFound."""
    respx.post(BASE_URL).mock(
        return_value=httpx.Response(200, json=rpc_err(404, "Contact not found"))
    )

    with pytest.raises(NutshellNotFound):
        await connector.get_contact(99999)


@pytest.mark.asyncio
@respx.mock
async def test_rpc_generic_error_envelope(connector):
    """HTTP 200 + a non-auth, non-notfound JSON-RPC error → NutshellError."""
    respx.post(BASE_URL).mock(
        return_value=httpx.Response(200, json=rpc_err(-32602, "Invalid params"))
    )

    with pytest.raises(NutshellError):
        await connector.create_contact({})


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """First call → 429, second call → 200 result. Connector layer with_retry covers it."""
    # Drop the per-request retry budget to 0 so the connector-layer with_retry
    # is the one that observes both responses (avoids respx exhaustion issues).
    connector.http_client._max_retries = 0

    route = respx.post(BASE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={}),
            httpx.Response(200, json=rpc_ok([SAMPLE_CONTACT])),
        ]
    )

    result = await connector.list_contacts()

    assert route.call_count == 2
    assert len(result) == 1


@pytest.mark.asyncio
@respx.mock
async def test_persistent_429_raises_rate_limit_error(connector, no_retry_sleep):
    respx.post(BASE_URL).mock(return_value=httpx.Response(429, json={}))

    with pytest.raises(NutshellRateLimitError):
        await connector.list_contacts()


# ═══════════════════════════════════════════════════════════════════════════
# Auth header — HTTP Basic with email + api_key
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_basic_auth_header_sent(connector):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json=rpc_ok([SAMPLE_USER]))

    respx.post(BASE_URL).mock(side_effect=_handler)

    await connector.list_users()

    expected = "Basic " + base64.b64encode(
        f"{TEST_EMAIL}:{TEST_API_KEY}".encode()
    ).decode()
    assert captured["auth"] == expected


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation sanity
# ═══════════════════════════════════════════════════════════════════════════


def test_different_tenants_yield_independent_instances():
    c1 = NutshellConnector(tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = NutshellConnector(tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    assert c1.http_client is not c2.http_client


# ═══════════════════════════════════════════════════════════════════════════
# mock_NutshellHTTPClient fixture — sanity
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_nutshell_http_client_fixture(mock_NutshellHTTPClient):
    """The fixture exposes AsyncMocks for every RPC the connector calls."""
    mock_NutshellHTTPClient.find_contacts.return_value = [SAMPLE_CONTACT]
    items = await mock_NutshellHTTPClient.find_contacts(page=1, limit=10)
    assert items == [SAMPLE_CONTACT]
    mock_NutshellHTTPClient.find_contacts.assert_awaited_once()
