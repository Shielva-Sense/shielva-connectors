"""Unit tests for DripConnector — respx-mocked, zero real I/O.

Coverage map:
    install / health
        - test_install_success
        - test_install_auth_error
        - test_install_missing_credentials
        - test_install_missing_account_id
        - test_health_check_ok
        - test_health_check_auth_error

    auth-header shape
        - test_authorization_header_is_basic_base64_token

    subscribers
        - test_list_subscribers_with_date_filter
        - test_get_subscriber_by_email
        - test_get_subscriber_by_id
        - test_get_subscriber_not_found
        - test_create_or_update_subscriber_envelope
        - test_delete_subscriber

    tags
        - test_list_tags
        - test_apply_tag_envelope
        - test_remove_tag

    events
        - test_record_event_envelope

    orders
        - test_list_orders
        - test_create_order_envelope

    campaigns
        - test_list_campaigns
        - test_get_campaign
        - test_subscribe_to_campaign

    workflows / custom fields / broadcasts / forms
        - test_list_workflows
        - test_trigger_workflow
        - test_list_custom_fields
        - test_list_broadcasts
        - test_list_forms

    retry / connector identity
        - test_retry_on_429_then_success
        - test_retry_on_500_then_success
        - test_connector_type_class_attr
        - test_auth_type_class_attr
        - test_required_config_keys_defined
        - test_independent_instances_per_tenant
        - test_legacy_api_token_config_key_still_works
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import DripConnector
from exceptions import DripAuthError, DripNotFoundError

from tests.conftest import (
    ACCOUNT_ID,
    API_KEY,
    CONNECTOR_ID,
    DRIP_BASE,
    TEST_CONFIG,
    TENANT_ID,
)


def _basic_auth() -> str:
    return "Basic " + base64.b64encode(f"{API_KEY}:".encode()).decode()


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_install_success(connector):
    route = respx.get(f"{DRIP_BASE}/campaigns").mock(
        return_value=httpx.Response(200, json={"campaigns": []}),
    )
    result = await connector.install()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID
    # Verify Drip's Basic auth header was sent verbatim
    sent_auth = route.calls.last.request.headers.get("Authorization")
    assert sent_auth == _basic_auth()


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_error(connector):
    respx.get(f"{DRIP_BASE}/campaigns").mock(
        return_value=httpx.Response(
            401,
            json={"errors": [{"message": "Invalid API token"}]},
        ),
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_credentials(connector):
    connector.config.pop("api_key", None)
    connector.config.pop("api_token", None)
    connector.api_key = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_account_id(connector):
    connector.config.pop("account_id", None)
    connector.account_id = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_authorization_header_is_basic_base64_token(connector):
    """Connector must Basic-auth using api_key as username + empty password."""
    route = respx.get(f"{DRIP_BASE}/campaigns").mock(
        return_value=httpx.Response(200, json={"campaigns": []}),
    )
    await connector.list_campaigns(page=1, per_page=1)
    assert route.called
    sent = route.calls.last.request.headers.get("Authorization")
    assert sent == _basic_auth()
    assert sent.startswith("Basic ")
    # Verify Content-Type is the JSON:API media type
    assert route.calls.last.request.headers.get("Content-Type") == "application/vnd.api+json"


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_ok(connector):
    respx.get(f"{DRIP_BASE}/campaigns").mock(
        return_value=httpx.Response(200, json={"campaigns": []}),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error(connector):
    respx.get(f"{DRIP_BASE}/campaigns").mock(
        return_value=httpx.Response(401, json={"errors": [{"message": "bad token"}]}),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


# ═══════════════════════════════════════════════════════════════════════════
# Subscribers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_subscribers_with_date_filter(connector):
    route = respx.get(f"{DRIP_BASE}/subscribers").mock(
        return_value=httpx.Response(
            200,
            json={"subscribers": [{"id": "1", "email": "a@x.com"}], "meta": {"total_pages": 1}},
        ),
    )
    result = await connector.list_subscribers(
        status="active",
        page=1,
        per_page=25,
        subscribed_after="2026-01-01T00:00:00Z",
        tags="vip",
    )
    assert route.called
    qs = route.calls.last.request.url.params
    assert qs["status"] == "active"
    assert qs["per_page"] == "25"
    assert qs["subscribed_after"] == "2026-01-01T00:00:00Z"
    assert qs["tags"] == "vip"
    assert result["subscribers"][0]["email"] == "a@x.com"


@pytest.mark.asyncio
@respx.mock
async def test_get_subscriber_by_email(connector):
    # '@' must encode to %40
    route = respx.get(f"{DRIP_BASE}/subscribers/user%40example.com").mock(
        return_value=httpx.Response(200, json={"subscribers": [{"email": "user@example.com"}]}),
    )
    result = await connector.get_subscriber("user@example.com")
    assert route.called
    assert result["subscribers"][0]["email"] == "user@example.com"


@pytest.mark.asyncio
@respx.mock
async def test_get_subscriber_by_id(connector):
    route = respx.get(f"{DRIP_BASE}/subscribers/abc123").mock(
        return_value=httpx.Response(200, json={"subscribers": [{"id": "abc123"}]}),
    )
    result = await connector.get_subscriber("abc123")
    assert route.called
    assert result["subscribers"][0]["id"] == "abc123"


@pytest.mark.asyncio
@respx.mock
async def test_get_subscriber_not_found(connector):
    respx.get(f"{DRIP_BASE}/subscribers/missing%40x.com").mock(
        return_value=httpx.Response(404, json={"errors": [{"message": "not found"}]}),
    )
    with pytest.raises(DripNotFoundError):
        await connector.get_subscriber("missing@x.com")


@pytest.mark.asyncio
@respx.mock
async def test_create_or_update_subscriber_envelope(connector):
    route = respx.post(f"{DRIP_BASE}/subscribers").mock(
        return_value=httpx.Response(
            201,
            json={"subscribers": [{"id": "new1", "email": "new@x.com"}]},
        ),
    )
    await connector.create_or_update_subscriber(
        email="new@x.com",
        custom_fields={"plan": "pro"},
        tags=["beta"],
        time_zone="America/Los_Angeles",
        first_name="Ada",
        last_name="Lovelace",
    )
    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert list(sent_body.keys()) == ["subscribers"]
    assert len(sent_body["subscribers"]) == 1
    sub = sent_body["subscribers"][0]
    assert sub["email"] == "new@x.com"
    assert sub["custom_fields"] == {"plan": "pro"}
    assert sub["tags"] == ["beta"]
    assert sub["time_zone"] == "America/Los_Angeles"
    assert sub["first_name"] == "Ada"
    assert sub["last_name"] == "Lovelace"


@pytest.mark.asyncio
@respx.mock
async def test_delete_subscriber(connector):
    route = respx.delete(f"{DRIP_BASE}/subscribers/del%40x.com").mock(
        return_value=httpx.Response(204),
    )
    result = await connector.delete_subscriber("del@x.com")
    assert route.called
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Tags
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_tags(connector):
    respx.get(f"{DRIP_BASE}/tags").mock(
        return_value=httpx.Response(200, json={"tags": ["vip", "trial"]}),
    )
    result = await connector.list_tags()
    assert result["tags"] == ["vip", "trial"]


@pytest.mark.asyncio
@respx.mock
async def test_apply_tag_envelope(connector):
    route = respx.post(f"{DRIP_BASE}/tags").mock(
        return_value=httpx.Response(201, json={}),
    )
    await connector.apply_tag(email="x@y.com", tag="vip")
    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == {"tags": [{"email": "x@y.com", "tag": "vip"}]}


@pytest.mark.asyncio
@respx.mock
async def test_remove_tag(connector):
    route = respx.delete(
        f"{DRIP_BASE}/subscribers/x%40y.com/tags/vip"
    ).mock(return_value=httpx.Response(204))
    await connector.remove_tag(email="x@y.com", tag="vip")
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Events
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_record_event_envelope(connector):
    route = respx.post(f"{DRIP_BASE}/events").mock(
        return_value=httpx.Response(204),
    )
    await connector.record_event(
        email="buyer@x.com",
        action="purchased",
        properties={"sku": "T-001", "amount": 99.0},
        occurred_at="2026-06-21T10:00:00Z",
    )
    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert list(sent_body.keys()) == ["events"]
    ev = sent_body["events"][0]
    assert ev["email"] == "buyer@x.com"
    assert ev["action"] == "purchased"
    assert ev["properties"]["sku"] == "T-001"
    assert ev["occurred_at"] == "2026-06-21T10:00:00Z"


# ═══════════════════════════════════════════════════════════════════════════
# Orders
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_orders(connector):
    route = respx.get(f"{DRIP_BASE}/orders").mock(
        return_value=httpx.Response(200, json={"orders": [{"id": "ord-1"}]}),
    )
    result = await connector.list_orders(page=2, per_page=25, occurred_after="2026-06-01T00:00:00Z")
    assert route.called
    qs = route.calls.last.request.url.params
    assert qs["page"] == "2"
    assert qs["per_page"] == "25"
    assert qs["occurred_after"] == "2026-06-01T00:00:00Z"
    assert result["orders"][0]["id"] == "ord-1"


@pytest.mark.asyncio
@respx.mock
async def test_create_order_envelope(connector):
    route = respx.post(f"{DRIP_BASE}/orders").mock(
        return_value=httpx.Response(201, json={"orders": [{"id": "ord-new"}]}),
    )
    await connector.create_order(
        email="b@x.com",
        provider="shopify",
        provider_order_id="A-1001",
        amount=4999,
        currency="USD",
        items=[{"name": "Mug", "sku": "M-01", "quantity": 1}],
    )
    sent_body = json.loads(route.calls.last.request.content)
    assert list(sent_body.keys()) == ["orders"]
    order = sent_body["orders"][0]
    assert order["email"] == "b@x.com"
    assert order["provider"] == "shopify"
    assert order["provider_order_id"] == "A-1001"
    assert order["amount"] == 4999
    assert order["currency"] == "USD"
    assert order["items"][0]["sku"] == "M-01"


# ═══════════════════════════════════════════════════════════════════════════
# Campaigns
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_campaigns(connector):
    route = respx.get(f"{DRIP_BASE}/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={"campaigns": [{"id": 7, "name": "Welcome", "status": "active"}]},
        ),
    )
    result = await connector.list_campaigns(status="active", page=1, per_page=10)
    assert route.called
    assert route.calls.last.request.url.params["status"] == "active"
    assert result["campaigns"][0]["name"] == "Welcome"


@pytest.mark.asyncio
@respx.mock
async def test_get_campaign(connector):
    route = respx.get(f"{DRIP_BASE}/campaigns/42").mock(
        return_value=httpx.Response(200, json={"campaigns": [{"id": 42}]}),
    )
    result = await connector.get_campaign(42)
    assert route.called
    assert result["campaigns"][0]["id"] == 42


@pytest.mark.asyncio
@respx.mock
async def test_subscribe_to_campaign(connector):
    route = respx.post(f"{DRIP_BASE}/campaigns/42/subscribers").mock(
        return_value=httpx.Response(201, json={"subscribers": [{"email": "s@x.com"}]}),
    )
    await connector.subscribe_to_campaign(
        campaign_id=42,
        email="s@x.com",
        double_optin=True,
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {"subscribers": [{"email": "s@x.com", "double_optin": True}]}


# ═══════════════════════════════════════════════════════════════════════════
# Workflows + custom fields + broadcasts + forms
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_workflows(connector):
    respx.get(f"{DRIP_BASE}/workflows").mock(
        return_value=httpx.Response(200, json={"workflows": [{"id": 1, "name": "Welcome"}]}),
    )
    result = await connector.list_workflows()
    assert result["workflows"][0]["id"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_trigger_workflow(connector):
    route = respx.post(f"{DRIP_BASE}/workflows/13/subscribers").mock(
        return_value=httpx.Response(202, json={}),
    )
    await connector.trigger_workflow(workflow_id=13, email="wf@x.com")
    body = json.loads(route.calls.last.request.content)
    assert body == {"subscribers": [{"email": "wf@x.com"}]}


@pytest.mark.asyncio
@respx.mock
async def test_list_custom_fields(connector):
    respx.get(f"{DRIP_BASE}/custom_field_identifiers").mock(
        return_value=httpx.Response(200, json={"custom_field_identifiers": ["plan", "tier"]}),
    )
    result = await connector.list_custom_fields()
    assert result["custom_field_identifiers"] == ["plan", "tier"]


@pytest.mark.asyncio
@respx.mock
async def test_list_broadcasts(connector):
    route = respx.get(f"{DRIP_BASE}/broadcasts").mock(
        return_value=httpx.Response(200, json={"broadcasts": []}),
    )
    await connector.list_broadcasts(status="draft", page=2)
    qs = route.calls.last.request.url.params
    assert qs["status"] == "draft"
    assert qs["page"] == "2"


@pytest.mark.asyncio
@respx.mock
async def test_list_forms(connector):
    respx.get(f"{DRIP_BASE}/forms").mock(
        return_value=httpx.Response(200, json={"forms": [{"id": 1}]}),
    )
    result = await connector.list_forms()
    assert result["forms"][0]["id"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Retry — 429 and 5xx converge to success
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{DRIP_BASE}/campaigns").mock(
        side_effect=[
            httpx.Response(429, json={"errors": [{"message": "slow down"}]}),
            httpx.Response(200, json={"campaigns": [{"id": 1}]}),
        ]
    )
    result = await connector.list_campaigns()
    assert route.call_count == 2
    assert result["campaigns"][0]["id"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{DRIP_BASE}/campaigns").mock(
        side_effect=[
            httpx.Response(500, json={"errors": [{"message": "boom"}]}),
            httpx.Response(200, json={"campaigns": []}),
        ]
    )
    result = await connector.list_campaigns()
    assert route.call_count == 2
    assert result == {"campaigns": []}


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity + multi-tenant + back-compat
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert DripConnector.CONNECTOR_TYPE == "drip"


def test_auth_type_class_attr():
    assert DripConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(DripConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in DripConnector.REQUIRED_CONFIG_KEYS
    assert "account_id" in DripConnector.REQUIRED_CONFIG_KEYS


def test_independent_instances_per_tenant():
    c1 = DripConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = DripConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_legacy_api_token_config_key_still_works():
    """Older installs persisted ``api_token`` instead of ``api_key`` — both must work."""
    legacy_cfg = {
        "api_token": "legacy-token",
        "account_id": ACCOUNT_ID,
        "base_url": "https://api.getdrip.com/v2",
    }
    c = DripConnector(tenant_id="t", connector_id="c", config=legacy_cfg)
    assert c.api_key == "legacy-token"
