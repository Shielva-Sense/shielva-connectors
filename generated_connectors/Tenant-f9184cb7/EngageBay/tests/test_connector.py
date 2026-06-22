"""Unit tests for EngageBayConnector — respx-mocked, zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import EngageBayConnector
from exceptions import EngageBayAuthError, EngageBayError, EngageBayNotFound

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    SAMPLE_CONTACT,
    SAMPLE_DEAL,
    SAMPLE_TASK,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# Class-level contract — CONNECTOR_TYPE / AUTH_TYPE / REQUIRED_CONFIG_KEYS
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert EngageBayConnector.CONNECTOR_TYPE == "engagebay"


def test_auth_type_class_attr():
    assert EngageBayConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(EngageBayConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in EngageBayConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(EngageBayConnector, "_STATUS_MAP")
    assert 401 in EngageBayConnector._STATUS_MAP
    assert 403 in EngageBayConnector._STATUS_MAP
    assert 429 in EngageBayConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_install_success(connector):
    respx.get(f"{BASE_URL}/subusers/list").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "email": "owner@example.com"}])
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_install_auth_error(connector):
    respx.get(f"{BASE_URL}/subusers/list").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_token(connector):
    token = await connector.authorize()
    assert token.access_token == TEST_API_KEY
    assert token.token_type == "api_key"
    assert token.expires_at is None


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_ok(connector):
    respx.get(f"{BASE_URL}/subusers/list").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_offline(connector):
    respx.get(f"{BASE_URL}/subusers/list").mock(
        return_value=httpx.Response(401, json={"message": "Invalid key"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_unhealthy(connector):
    respx.get(f"{BASE_URL}/subusers/list").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# auth-header contract — Authorization is the bare key (no Bearer prefix)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bare_key(connector):
    route = respx.get(f"{BASE_URL}/subusers/list").mock(
        return_value=httpx.Response(200, json=[])
    )
    await connector.health_check()
    sent_auth = route.calls.last.request.headers.get("Authorization")
    assert sent_auth == TEST_API_KEY
    assert not sent_auth.lower().startswith("bearer ")


# ═══════════════════════════════════════════════════════════════════════════
# list_contacts() — pagination via cursor
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_first_page(connector):
    respx.get(f"{BASE_URL}/contacts").mock(
        return_value=httpx.Response(
            200, json={"items": [SAMPLE_CONTACT], "cursor": "next-cursor-1"}
        )
    )
    resp = await connector.list_contacts(page_size=50)
    assert isinstance(resp, dict)
    assert resp["items"][0]["id"] == SAMPLE_CONTACT["id"]
    assert resp["cursor"] == "next-cursor-1"


@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_with_cursor(connector):
    route = respx.get(f"{BASE_URL}/contacts").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None})
    )
    await connector.list_contacts(page_size=25, page_cursor="next-cursor-1")
    assert route.called
    qs = route.calls.last.request.url.params
    assert qs.get("page_size") == "25"
    assert qs.get("page_cursor") == "next-cursor-1"


# ═══════════════════════════════════════════════════════════════════════════
# get_contact() — normalizes to flat dict
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_contact_normalized(connector):
    respx.get(f"{BASE_URL}/contacts/5001").mock(
        return_value=httpx.Response(200, json=SAMPLE_CONTACT)
    )
    contact = await connector.get_contact("5001")
    assert contact["id"] == "5001"
    assert contact["email"] == "ada@example.com"
    assert contact["first_name"] == "Ada"
    assert "vip" in contact["tags"]


@respx.mock
@pytest.mark.asyncio
async def test_get_contact_not_found(connector):
    respx.get(f"{BASE_URL}/contacts/missing").mock(
        return_value=httpx.Response(404, json={"message": "no such contact"})
    )
    with pytest.raises(EngageBayNotFound):
        await connector.get_contact("missing")


# ═══════════════════════════════════════════════════════════════════════════
# create_contact() — properties array shape
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_create_contact_sends_properties_array(connector):
    route = respx.post(f"{BASE_URL}/contacts").mock(
        return_value=httpx.Response(200, json={"id": 5002})
    )
    resp = await connector.create_contact(
        properties=[
            {"name": "email", "value": "grace@example.com"},
            {"name": "first_name", "value": "Grace", "field_type": "TEXT"},
        ]
    )
    assert resp["id"] == 5002
    parsed = _json.loads(route.calls.last.request.content)
    assert "properties" in parsed
    assert parsed["properties"][0]["name"] == "email"
    # default field_type should be applied
    assert parsed["properties"][0]["field_type"] == "TEXT"


@pytest.mark.asyncio
async def test_create_contact_rejects_empty_properties(connector):
    with pytest.raises(ValueError):
        await connector.create_contact(properties=[])


# ═══════════════════════════════════════════════════════════════════════════
# update_contact()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_update_contact_partial(connector):
    route = respx.put(f"{BASE_URL}/contacts/update-partial/5001").mock(
        return_value=httpx.Response(200, json={"id": 5001, "updated": True})
    )
    resp = await connector.update_contact(
        contact_id="5001",
        properties=[{"name": "phone", "value": "+15555550199"}],
    )
    assert resp["updated"] is True
    assert route.called
    body = _json.loads(route.calls.last.request.content)
    assert body["id"] == "5001"
    assert body["properties"][0]["name"] == "phone"


@respx.mock
@pytest.mark.asyncio
async def test_delete_contact(connector):
    route = respx.delete(f"{BASE_URL}/contacts/5001").mock(
        return_value=httpx.Response(204)
    )
    resp = await connector.delete_contact("5001")
    assert route.called
    assert resp == {}


# ═══════════════════════════════════════════════════════════════════════════
# Companies
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_companies(connector):
    route = respx.get(f"{BASE_URL}/companies/list/25").mock(
        return_value=httpx.Response(200, json={"items": [{"id": 9999, "name": "Acme"}]})
    )
    resp = await connector.list_companies(page_size=25)
    assert route.called
    assert resp["items"][0]["name"] == "Acme"


# ═══════════════════════════════════════════════════════════════════════════
# list_deals + create_deal
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_deals(connector):
    respx.get(f"{BASE_URL}/deals").mock(
        return_value=httpx.Response(200, json={"items": [SAMPLE_DEAL]})
    )
    resp = await connector.list_deals(page_size=10)
    assert resp["items"][0]["name"] == "Acme renewal"


@respx.mock
@pytest.mark.asyncio
async def test_create_deal(connector):
    route = respx.post(f"{BASE_URL}/deals").mock(
        return_value=httpx.Response(200, json=SAMPLE_DEAL)
    )
    resp = await connector.create_deal(
        name="Acme renewal",
        expected_value=12500.0,
        milestone="Negotiation",
        contact_ids=["5001"],
    )
    assert resp["id"] == 9001
    body = _json.loads(route.calls.last.request.content)
    assert body["name"] == "Acme renewal"
    assert body["expected_value"] == 12500.0
    assert body["milestone"] == "Negotiation"
    assert body["contact_ids"] == ["5001"]


# ═══════════════════════════════════════════════════════════════════════════
# list_tasks + create_task
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_tasks(connector):
    respx.get(f"{BASE_URL}/tasks").mock(
        return_value=httpx.Response(200, json={"items": [SAMPLE_TASK]})
    )
    resp = await connector.list_tasks(page_size=10)
    assert resp["items"][0]["name"] == "Follow up with Ada"


@respx.mock
@pytest.mark.asyncio
async def test_create_task(connector):
    route = respx.post(f"{BASE_URL}/tasks").mock(
        return_value=httpx.Response(200, json=SAMPLE_TASK)
    )
    resp = await connector.create_task(
        name="Follow up with Ada",
        due_date=1900000000000,
        contact_id="5001",
        owner_id=42,
    )
    assert resp["id"] == 7001
    body = _json.loads(route.calls.last.request.content)
    assert body["name"] == "Follow up with Ada"
    assert body["contact_id"] == "5001"
    assert body["owner_id"] == 42


# ═══════════════════════════════════════════════════════════════════════════
# list_tickets
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_tickets(connector):
    respx.get(f"{BASE_URL}/tickets").mock(
        return_value=httpx.Response(200, json={"items": [{"id": 1, "subject": "Help"}]})
    )
    resp = await connector.list_tickets(page_size=5)
    assert resp["items"][0]["subject"] == "Help"


# ═══════════════════════════════════════════════════════════════════════════
# add_note
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_add_note(connector):
    route = respx.post(f"{BASE_URL}/contacts/5001/note").mock(
        return_value=httpx.Response(200, json={"id": 60001, "note": "Spoke today"})
    )
    resp = await connector.add_note(contact_id="5001", note="Spoke today")
    assert resp["id"] == 60001
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# sync() — walks /contacts and ingests NormalizedDocument
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_sync_ingests_contacts(connector):
    respx.get(f"{BASE_URL}/contacts").mock(
        return_value=httpx.Response(200, json={"items": [SAMPLE_CONTACT], "cursor": None})
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@respx.mock
@pytest.mark.asyncio
async def test_sync_no_documents(connector):
    respx.get(f"{BASE_URL}/contacts").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None})
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/subusers/list").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
            httpx.Response(200, json=[{"id": 1}]),
        ]
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/subusers/list").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json=[]),
        ]
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# mock_EngageBayHTTPClient fixture — verifies the injection point
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_mock_http_client_fixture(mock_EngageBayHTTPClient):
    mock_EngageBayHTTPClient.get.return_value = {"items": [], "cursor": None}
    c = EngageBayConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
    resp = await c.list_contacts(page_size=10)
    assert resp == {"items": [], "cursor": None}
    mock_EngageBayHTTPClient.get.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = EngageBayConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = EngageBayConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — NormalizedDocument id is tenant-scoped
# ═══════════════════════════════════════════════════════════════════════════

def test_normalized_doc_id_is_tenant_scoped():
    from helpers.normalizer import normalize_contact_doc

    doc = normalize_contact_doc(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.id == f"{TENANT_ID}_5001"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.source == "engagebay"
    assert doc.metadata["kind"] == "engagebay.contact"
