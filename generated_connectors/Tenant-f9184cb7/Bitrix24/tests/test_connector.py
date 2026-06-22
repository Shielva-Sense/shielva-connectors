"""Unit tests for Bitrix24Connector — respx-mocked, zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import Bitrix24Connector
from exceptions import (
    Bitrix24AuthError,
    Bitrix24Error,
    Bitrix24NotFound,
    Bitrix24RateLimitError,
)

from tests.conftest import (
    CONNECTOR_ID,
    TENANT_ID,
    TEST_CONFIG,
    WEBHOOK_BASE,
    WEBHOOK_URL,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_webhook_url(connector):
    connector.config.pop("webhook_url", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_invalid_webhook_url(connector):
    connector.config["webhook_url"] = "not-a-url"
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_oauth_mode_with_access_token(mocker):
    """Mode B — access_token + portal succeeds even without webhook_url."""
    config = {
        "webhook_url": "",
        "access_token": "fake-oauth-token",
        "portal": "mycompany",
    }
    c = Bitrix24Connector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=config,
    )
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header / URL construction
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_webhook_url_is_used_as_credential(connector):
    """Connector must POST to `{webhook_url}{method}.json` — no auth query param."""
    url = f"{WEBHOOK_BASE}/user.current.json"
    route = respx.post(url).mock(
        return_value=httpx.Response(200, json={"result": {"ID": 1, "NAME": "Bob"}})
    )
    await connector.user_current()
    assert route.called
    sent = route.calls[0].request
    # No `auth=` query param in webhook mode
    assert "auth=" not in str(sent.url.query)


@respx.mock
@pytest.mark.asyncio
async def test_oauth_mode_appends_auth_query_param():
    """Mode B — connector must append `?auth=<token>` to the URL."""
    config = {
        "access_token": "oauth-token-xyz",
        "portal": "mycompany",
        "base_url": "https://mycompany.bitrix24.com/rest",
    }
    c = Bitrix24Connector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=config,
    )
    route = respx.post("https://mycompany.bitrix24.com/rest/user.current.json").mock(
        return_value=httpx.Response(200, json={"result": {"ID": 1}})
    )
    await c.user_current()
    assert route.called
    sent_qs = route.calls[0].request.url.params
    assert sent_qs.get("auth") == "oauth-token-xyz"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises(connector):
    respx.post(f"{WEBHOOK_BASE}/user.current.json").mock(
        return_value=httpx.Response(401, json={"error": "Unauthorized"})
    )
    with pytest.raises(Bitrix24AuthError):
        await connector.user_current()


@respx.mock
@pytest.mark.asyncio
async def test_embedded_expired_token_raises_auth_error(connector):
    """HTTP 200 with `{"error": "expired_token"}` body must raise Bitrix24AuthError."""
    respx.post(f"{WEBHOOK_BASE}/user.current.json").mock(
        return_value=httpx.Response(
            200,
            json={"error": "expired_token", "error_description": "token expired"},
        )
    )
    with pytest.raises(Bitrix24AuthError):
        await connector.user_current()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.post(f"{WEBHOOK_BASE}/user.current.json").mock(
        return_value=httpx.Response(200, json={"result": {"ID": 1}})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.post(f"{WEBHOOK_BASE}/user.current.json").mock(
        return_value=httpx.Response(401, json={"error": "Invalid webhook"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# CRM Leads
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_leads_posts_payload(connector):
    route = respx.post(f"{WEBHOOK_BASE}/crm.lead.list.json").mock(
        return_value=httpx.Response(
            200,
            json={"result": [{"ID": "1", "TITLE": "Lead A"}], "total": 1},
        )
    )
    result = await connector.list_leads(
        start=0, filter={"STATUS_ID": "NEW"}, select=["ID", "TITLE"],
    )
    assert route.called
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["start"] == 0
    assert body["filter"] == {"STATUS_ID": "NEW"}
    assert body["select"] == ["ID", "TITLE"]
    assert result["result"][0]["TITLE"] == "Lead A"


@respx.mock
@pytest.mark.asyncio
async def test_get_lead_success(connector):
    respx.post(f"{WEBHOOK_BASE}/crm.lead.get.json").mock(
        return_value=httpx.Response(200, json={"result": {"ID": "42", "TITLE": "L"}})
    )
    result = await connector.get_lead(42)
    assert result["result"]["ID"] == "42"


@respx.mock
@pytest.mark.asyncio
async def test_create_lead_posts_fields_envelope(connector):
    route = respx.post(f"{WEBHOOK_BASE}/crm.lead.add.json").mock(
        return_value=httpx.Response(200, json={"result": 99})
    )
    fields = {"TITLE": "New", "NAME": "Ada"}
    result = await connector.create_lead(fields)
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == {"fields": fields}
    assert result["result"] == 99


# ═══════════════════════════════════════════════════════════════════════════
# CRM Contacts
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_default_select(connector):
    route = respx.post(f"{WEBHOOK_BASE}/crm.contact.list.json").mock(
        return_value=httpx.Response(
            200,
            json={"result": [{"ID": "1", "NAME": "Bob"}], "total": 1},
        )
    )
    await connector.list_contacts(start=0)
    body = _json.loads(route.calls[0].request.content.decode())
    assert "PHONE" in body["select"]
    assert "EMAIL" in body["select"]


@respx.mock
@pytest.mark.asyncio
async def test_create_contact_normalizes_phone_and_email(connector):
    route = respx.post(f"{WEBHOOK_BASE}/crm.contact.add.json").mock(
        return_value=httpx.Response(200, json={"result": 42})
    )
    result = await connector.create_contact(
        name="Ada",
        last_name="Lovelace",
        phone=["+1-555-0100"],
        email=["ada@example.com"],
    )
    body = _json.loads(route.calls[0].request.content.decode())
    fields = body["fields"]
    assert fields["NAME"] == "Ada"
    assert fields["LAST_NAME"] == "Lovelace"
    assert fields["PHONE"] == [{"VALUE": "+1-555-0100", "VALUE_TYPE": "WORK"}]
    assert fields["EMAIL"] == [{"VALUE": "ada@example.com", "VALUE_TYPE": "WORK"}]
    assert result["result"] == 42


# ═══════════════════════════════════════════════════════════════════════════
# CRM Deals + Companies
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_deals_success(connector):
    respx.post(f"{WEBHOOK_BASE}/crm.deal.list.json").mock(
        return_value=httpx.Response(
            200,
            json={"result": [{"ID": "10", "TITLE": "Deal A"}], "total": 1},
        )
    )
    result = await connector.list_deals(start=0)
    assert result["result"][0]["TITLE"] == "Deal A"


@respx.mock
@pytest.mark.asyncio
async def test_create_deal_posts_fields_envelope(connector):
    route = respx.post(f"{WEBHOOK_BASE}/crm.deal.add.json").mock(
        return_value=httpx.Response(200, json={"result": 7})
    )
    fields = {"TITLE": "Big Deal", "OPPORTUNITY": 1000, "CURRENCY_ID": "USD"}
    await connector.create_deal(fields)
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == {"fields": fields}


@respx.mock
@pytest.mark.asyncio
async def test_list_companies_success(connector):
    respx.post(f"{WEBHOOK_BASE}/crm.company.list.json").mock(
        return_value=httpx.Response(200, json={"result": [{"ID": "1"}], "total": 1})
    )
    result = await connector.list_companies()
    assert result["result"][0]["ID"] == "1"


# ═══════════════════════════════════════════════════════════════════════════
# Tasks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_tasks_returns_nested_result(connector):
    respx.post(f"{WEBHOOK_BASE}/tasks.task.list.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "tasks": [{"id": "1", "title": "Task A"}, {"id": "2", "title": "Task B"}]
                }
            },
        )
    )
    result = await connector.list_tasks(start=0)
    assert result["result"]["tasks"][0]["title"] == "Task A"


@respx.mock
@pytest.mark.asyncio
async def test_create_task_builds_fields_envelope(connector):
    route = respx.post(f"{WEBHOOK_BASE}/tasks.task.add.json").mock(
        return_value=httpx.Response(200, json={"result": {"task": {"id": "1"}}})
    )
    await connector.create_task(
        title="My Task", responsible_id=5, description="Do it"
    )
    body = _json.loads(route.calls[0].request.content.decode())
    fields = body["fields"]
    assert fields["TITLE"] == "My Task"
    assert fields["RESPONSIBLE_ID"] == 5
    assert fields["DESCRIPTION"] == "Do it"


# ═══════════════════════════════════════════════════════════════════════════
# Im messaging
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_send_im_message_builds_payload(connector):
    route = respx.post(f"{WEBHOOK_BASE}/im.message.add.json").mock(
        return_value=httpx.Response(200, json={"result": 1234})
    )
    await connector.send_im_message(
        dialog_id="chat100", message="hello", system=True
    )
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["DIALOG_ID"] == "chat100"
    assert body["MESSAGE"] == "hello"
    assert body["SYSTEM"] == "Y"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.post(f"{WEBHOOK_BASE}/user.current.json").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json={"result": {"ID": 1}}),
        ]
    )
    result = await connector.user_current()
    assert route.call_count == 2
    assert result["result"]["ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.post(f"{WEBHOOK_BASE}/user.current.json").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json={"result": {"ID": 1}}),
        ]
    )
    result = await connector.user_current()
    assert route.call_count == 2
    assert result["result"]["ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_embedded_query_limit_exceeded(connector, no_retry_sleep):
    """Bitrix24's HTTP-200 `QUERY_LIMIT_EXCEEDED` envelope must trigger retry."""
    route = respx.post(f"{WEBHOOK_BASE}/user.current.json").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "error": "QUERY_LIMIT_EXCEEDED",
                    "error_description": "too many requests",
                },
            ),
            httpx.Response(200, json={"result": {"ID": 1}}),
        ]
    )
    result = await connector.user_current()
    assert route.call_count == 2
    assert result["result"]["ID"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 404 propagation
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_lead_not_found(connector):
    respx.post(f"{WEBHOOK_BASE}/crm.lead.get.json").mock(
        return_value=httpx.Response(404, json={"error": "lead not found"})
    )
    with pytest.raises(Bitrix24NotFound):
        await connector.get_lead(99999)


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert Bitrix24Connector.CONNECTOR_TYPE == "bitrix24"


def test_auth_type_class_attr():
    assert Bitrix24Connector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(Bitrix24Connector, "REQUIRED_CONFIG_KEYS")
    assert "webhook_url" in Bitrix24Connector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = Bitrix24Connector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = Bitrix24Connector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Pure helpers (normalizer + utils)
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_phone_and_email_helpers():
    from helpers.utils import normalize_email_list, normalize_phone_list

    assert normalize_phone_list(None) is None
    assert normalize_phone_list(["+1-555"]) == [
        {"VALUE": "+1-555", "VALUE_TYPE": "WORK"}
    ]
    assert normalize_email_list([{"VALUE": "a@b", "VALUE_TYPE": "HOME"}]) == [
        {"VALUE": "a@b", "VALUE_TYPE": "HOME"}
    ]


def test_extract_portal_from_webhook_url():
    from helpers.utils import extract_portal

    assert (
        extract_portal("https://mycompany.bitrix24.com/rest/1/abc/")
        == "mycompany"
    )
    assert extract_portal("") == ""


def test_normalize_lead_produces_tenant_scoped_id():
    from helpers.normalizer import normalize_lead

    doc = normalize_lead(
        {"ID": "42", "TITLE": "L", "NAME": "A", "LAST_NAME": "B", "STATUS_ID": "NEW"},
        connector_id="conn-X",
        tenant_id="tenant-Y",
    )
    assert doc.id == "tenant-Y_42"
    assert doc.source_id == "42"
    assert doc.metadata["kind"] == "bitrix24.lead"


def test_normalize_task_handles_camelcase_keys():
    from helpers.normalizer import normalize_task

    doc = normalize_task(
        {
            "id": "7",
            "title": "T",
            "description": "D",
            "responsibleId": 3,
            "status": "2",
        },
        connector_id="c",
        tenant_id="t",
    )
    assert doc.id == "t_7"
    assert doc.content == "D"
    assert doc.metadata["responsible_id"] == 3
