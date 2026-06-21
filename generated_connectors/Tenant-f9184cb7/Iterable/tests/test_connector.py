"""Unit tests for IterableConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import IterableConnector
from exceptions import (
    IterableAuthError,
    IterableBadRequestError,
    IterableError,
    IterableNotFound,
    IterableNotFoundError,
)

from tests.conftest import (
    API_KEY,
    BASE_URL,
    CONNECTOR_ID,
    EU_BASE_URL,
    TENANT_ID,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install() + class identity
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.connector_id == CONNECTOR_ID
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED
    assert "installed" in (result.message or "").lower()


@pytest.mark.asyncio
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    connector.api_key = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE
    assert "api_key" in (result.message or "")


def test_connector_type_class_attr():
    assert IterableConnector.CONNECTOR_TYPE == "iterable"


def test_auth_type_class_attr():
    assert IterableConnector.AUTH_TYPE == "api_key"


def test_required_config_keys():
    assert "api_key" in IterableConnector.REQUIRED_CONFIG_KEYS


def test_eu_region_picks_eu_base_url():
    c = IterableConnector(
        tenant_id="t-eu",
        connector_id="c-eu",
        config={"api_key": "k", "region": "eu"},
    )
    assert c.base_url == EU_BASE_URL


def test_us_region_default_picks_us_base_url():
    c = IterableConnector(
        tenant_id="t-us",
        connector_id="c-us",
        config={"api_key": "k"},
    )
    assert c.base_url == BASE_URL


def test_explicit_base_url_overrides_region():
    custom = "https://api.iterable.example/api"
    c = IterableConnector(
        tenant_id="t-c",
        connector_id="c-c",
        config={"api_key": "k", "region": "eu", "base_url": custom},
    )
    assert c.base_url == custom


def test_independent_instances_per_tenant():
    c1 = IterableConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = IterableConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Api-Key, not Authorization: Bearer)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_api_key_header_shape(connector):
    """Iterable expects `Api-Key: <key>`, NEVER `Authorization: Bearer ...`."""
    route = respx.get(f"{BASE_URL}/lists").mock(
        return_value=httpx.Response(200, json={"lists": []})
    )
    await connector.list_lists()
    assert route.called
    req = route.calls[0].request
    assert req.headers.get("Api-Key") == API_KEY
    # Iterable forbids the Bearer-style header
    auth = req.headers.get("Authorization", "")
    assert not auth.lower().startswith("bearer ")


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_api_key_token(connector):
    token = await connector.authorize()
    assert token.access_token == API_KEY
    assert token.token_type == "api_key"
    assert token.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{BASE_URL}/lists").mock(
        return_value=httpx.Response(200, json={"lists": []})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_offline(connector):
    respx.get(f"{BASE_URL}/lists").mock(
        return_value=httpx.Response(401, json={"msg": "Invalid API key"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_unhealthy(connector):
    respx.get(f"{BASE_URL}/lists").mock(
        return_value=httpx.Response(403, json={"msg": "Forbidden scope"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.UNHEALTHY
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_get_user_by_email_unwraps_envelope(connector):
    respx.get(f"{BASE_URL}/users/getByEmail").mock(
        return_value=httpx.Response(
            200, json={"user": {"email": "a@b.com", "userId": "u1"}}
        )
    )
    user = await connector.get_user(email="a@b.com")
    assert user["email"] == "a@b.com"
    assert user["userId"] == "u1"


@respx.mock
@pytest.mark.asyncio
async def test_get_user_by_user_id(connector):
    respx.get(f"{BASE_URL}/users/byUserId/u-42").mock(
        return_value=httpx.Response(200, json={"email": "x@y.com", "userId": "u-42"})
    )
    user = await connector.get_user(user_id="u-42")
    assert user["userId"] == "u-42"


@pytest.mark.asyncio
async def test_get_user_requires_identifier(connector):
    with pytest.raises(ValueError):
        await connector.get_user()


@respx.mock
@pytest.mark.asyncio
async def test_update_user_posts_identity_and_data_fields(connector):
    route = respx.post(f"{BASE_URL}/users/update").mock(
        return_value=httpx.Response(200, json={"code": "Success"})
    )
    out = await connector.update_user(
        email="user@example.com",
        data_fields={"firstName": "Ada"},
        merge_nested_objects=False,
    )
    assert out["code"] == "Success"
    body = json.loads(route.calls.last.request.content.decode())
    assert body["email"] == "user@example.com"
    assert body["dataFields"] == {"firstName": "Ada"}
    assert body["mergeNestedObjects"] is False


@respx.mock
@pytest.mark.asyncio
async def test_bulk_update_users(connector):
    route = respx.post(f"{BASE_URL}/users/bulkUpdate").mock(
        return_value=httpx.Response(200, json={"successCount": 2, "failCount": 0})
    )
    out = await connector.bulk_update_users(
        [
            {"email": "a@x.com", "dataFields": {"plan": "pro"}},
            {"email": "b@x.com", "dataFields": {"plan": "free"}},
        ]
    )
    assert out["successCount"] == 2
    body = json.loads(route.calls.last.request.content.decode())
    assert isinstance(body["users"], list)
    assert len(body["users"]) == 2


@pytest.mark.asyncio
async def test_bulk_update_users_rejects_empty(connector):
    with pytest.raises(ValueError):
        await connector.bulk_update_users([])


@respx.mock
@pytest.mark.asyncio
async def test_list_users_parses_newline_delimited_export(connector):
    body = "alice@x.com\nbob@x.com\ncarol@x.com\n"
    respx.get(f"{BASE_URL}/lists/getUsers").mock(
        return_value=httpx.Response(200, text=body)
    )
    emails = await connector.list_users(list_id=42)
    assert emails == ["alice@x.com", "bob@x.com", "carol@x.com"]


@respx.mock
@pytest.mark.asyncio
async def test_update_email_posts_current_and_new(connector):
    route = respx.post(f"{BASE_URL}/users/updateEmail").mock(
        return_value=httpx.Response(200, json={"code": "Success"})
    )
    out = await connector.update_email("old@x.com", "new@x.com")
    assert out["code"] == "Success"
    body = json.loads(route.calls.last.request.content.decode())
    assert body == {"currentEmail": "old@x.com", "newEmail": "new@x.com"}


@respx.mock
@pytest.mark.asyncio
async def test_delete_user_by_user_id_uses_byuserid_path(connector):
    route = respx.delete(f"{BASE_URL}/users/byUserId/u-99").mock(
        return_value=httpx.Response(200, json={"code": "Success"})
    )
    out = await connector.delete_user(user_id="u-99")
    assert out["code"] == "Success"
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Events
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_track_event_builds_correct_body(connector):
    route = respx.post(f"{BASE_URL}/events/track").mock(
        return_value=httpx.Response(200, json={"code": "Success"})
    )
    out = await connector.track_event(
        email="u@x.com",
        event_name="checkout_completed",
        data_fields={"value": 99.0},
        campaign_id=42,
        template_id=7,
    )
    assert out["code"] == "Success"
    body = json.loads(route.calls.last.request.content.decode())
    assert body["email"] == "u@x.com"
    assert body["eventName"] == "checkout_completed"
    assert body["dataFields"] == {"value": 99.0}
    assert body["campaignId"] == 42
    assert body["templateId"] == 7


@respx.mock
@pytest.mark.asyncio
async def test_bulk_track_events(connector):
    route = respx.post(f"{BASE_URL}/events/trackBulk").mock(
        return_value=httpx.Response(200, json={"successCount": 3})
    )
    out = await connector.bulk_track_events(
        [
            {"email": "a@x.com", "eventName": "view"},
            {"email": "b@x.com", "eventName": "click"},
            {"email": "c@x.com", "eventName": "buy"},
        ]
    )
    assert out["successCount"] == 3
    body = json.loads(route.calls.last.request.content.decode())
    assert len(body["events"]) == 3


# ═══════════════════════════════════════════════════════════════════════════
# Campaigns
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_campaigns_unwraps_envelope(connector):
    respx.get(f"{BASE_URL}/campaigns").mock(
        return_value=httpx.Response(
            200,
            json={"campaigns": [{"id": 1, "name": "Welcome"}, {"id": 2, "name": "Drip"}]},
        )
    )
    campaigns = await connector.list_campaigns()
    assert len(campaigns) == 2
    assert campaigns[0]["name"] == "Welcome"


@respx.mock
@pytest.mark.asyncio
async def test_get_campaign_falls_back_to_metrics_on_404(connector):
    respx.get(f"{BASE_URL}/campaigns/99").mock(
        return_value=httpx.Response(404, json={"msg": "not found"})
    )
    respx.get(f"{BASE_URL}/campaigns/metrics").mock(
        return_value=httpx.Response(200, json={"id": 99, "name": "Legacy"})
    )
    out = await connector.get_campaign(99)
    assert out["id"] == 99
    assert out["name"] == "Legacy"


@respx.mock
@pytest.mark.asyncio
async def test_create_triggered_campaign_builds_body(connector):
    route = respx.post(f"{BASE_URL}/campaigns/create").mock(
        return_value=httpx.Response(200, json={"campaignId": 555})
    )
    out = await connector.create_triggered_campaign(
        name="Black Friday",
        list_ids=[10, 20],
        template_id=7,
        suppression_list_ids=[99],
    )
    assert out["campaignId"] == 555
    body = json.loads(route.calls.last.request.content.decode())
    assert body["name"] == "Black Friday"
    assert body["listIds"] == [10, 20]
    assert body["templateId"] == 7
    assert body["suppressionListIds"] == [99]


# ═══════════════════════════════════════════════════════════════════════════
# Templates
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_templates_passes_query_params(connector):
    route = respx.get(f"{BASE_URL}/templates").mock(
        return_value=httpx.Response(
            200, json={"templates": [{"templateId": 5, "name": "Welcome"}]}
        )
    )
    out = await connector.list_templates(template_type="Blast", message_medium="SMS")
    assert out["templates"][0]["templateId"] == 5
    qs = route.calls.last.request.url.params
    assert qs.get("templateType") == "Blast"
    assert qs.get("messageMedium") == "SMS"


@respx.mock
@pytest.mark.asyncio
async def test_get_template_not_found(connector):
    respx.get(f"{BASE_URL}/templates/999").mock(
        return_value=httpx.Response(404, json={"msg": "template not found"})
    )
    with pytest.raises(IterableNotFound):
        await connector.get_template(999)


# ═══════════════════════════════════════════════════════════════════════════
# Channels
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_channels_unwraps(connector):
    respx.get(f"{BASE_URL}/channels").mock(
        return_value=httpx.Response(
            200, json={"channels": [{"id": 1, "name": "Marketing Email"}]}
        )
    )
    out = await connector.list_channels()
    assert out[0]["name"] == "Marketing Email"


# ═══════════════════════════════════════════════════════════════════════════
# Lists
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_lists(connector):
    respx.get(f"{BASE_URL}/lists").mock(
        return_value=httpx.Response(
            200, json={"lists": [{"id": 1, "name": "VIP"}, {"id": 2, "name": "Trial"}]}
        )
    )
    lists = await connector.list_lists()
    assert len(lists) == 2
    assert lists[0]["name"] == "VIP"


@respx.mock
@pytest.mark.asyncio
async def test_create_list(connector):
    respx.post(f"{BASE_URL}/lists").mock(
        return_value=httpx.Response(200, json={"listId": 99})
    )
    out = await connector.create_list("New segment")
    assert out["listId"] == 99


@respx.mock
@pytest.mark.asyncio
async def test_delete_list(connector):
    route = respx.delete(f"{BASE_URL}/lists/42").mock(
        return_value=httpx.Response(200, json={"code": "Success"})
    )
    out = await connector.delete_list(42)
    assert out["code"] == "Success"
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_subscribe_to_list_posts_list_id_and_subscribers(connector):
    route = respx.post(f"{BASE_URL}/lists/subscribe").mock(
        return_value=httpx.Response(
            200, json={"successCount": 1, "failCount": 0, "invalidEmails": []}
        )
    )
    out = await connector.subscribe_to_list(
        list_id=10, subscribers=[{"email": "new@x.com"}]
    )
    assert out["successCount"] == 1
    body = json.loads(route.calls.last.request.content.decode())
    assert body["listId"] == 10
    assert body["subscribers"] == [{"email": "new@x.com"}]


@respx.mock
@pytest.mark.asyncio
async def test_unsubscribe_from_list_includes_campaign_id(connector):
    route = respx.post(f"{BASE_URL}/lists/unsubscribe").mock(
        return_value=httpx.Response(200, json={"successCount": 1})
    )
    out = await connector.unsubscribe_from_list(
        list_id=10,
        subscribers=[{"email": "gone@x.com"}],
        campaign_id=42,
        channel_unsubscribe=True,
    )
    assert out["successCount"] == 1
    body = json.loads(route.calls.last.request.content.decode())
    assert body["listId"] == 10
    assert body["campaignId"] == 42
    assert body["channelUnsubscribe"] is True


@pytest.mark.asyncio
async def test_subscribe_rejects_subscriber_without_identifier(connector):
    with pytest.raises(ValueError):
        await connector.subscribe_to_list(
            list_id=1, subscribers=[{"firstName": "anon"}]
        )


# ═══════════════════════════════════════════════════════════════════════════
# Catalogs
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_catalogs_handles_legacy_shape(connector):
    respx.get(f"{BASE_URL}/catalogs").mock(
        return_value=httpx.Response(
            200, json={"params": {"catalogNames": ["products", "stores"]}}
        )
    )
    names = await connector.list_catalogs()
    assert names == ["products", "stores"]


@respx.mock
@pytest.mark.asyncio
async def test_list_catalogs_handles_modern_shape(connector):
    respx.get(f"{BASE_URL}/catalogs").mock(
        return_value=httpx.Response(
            200, json={"catalogs": [{"name": "products"}, {"name": "stores"}]}
        )
    )
    names = await connector.list_catalogs()
    assert names == ["products", "stores"]


@respx.mock
@pytest.mark.asyncio
async def test_upsert_catalog_item(connector):
    route = respx.put(f"{BASE_URL}/catalogs/products/items/SKU-1").mock(
        return_value=httpx.Response(200, json={"code": "Success"})
    )
    out = await connector.upsert_catalog_item("products", "SKU-1", {"price": 19.0})
    assert out["code"] == "Success"
    body = json.loads(route.calls.last.request.content.decode())
    assert body == {"value": {"price": 19.0}}


# ═══════════════════════════════════════════════════════════════════════════
# Workflows
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_trigger_workflow(connector):
    route = respx.post(f"{BASE_URL}/workflows/triggerWorkflow").mock(
        return_value=httpx.Response(200, json={"code": "Success"})
    )
    out = await connector.trigger_workflow(
        workflow_id=7, email="u@x.com", data_fields={"plan": "pro"}
    )
    assert out["code"] == "Success"
    body = json.loads(route.calls.last.request.content.decode())
    assert body["workflowId"] == 7
    assert body["email"] == "u@x.com"
    assert body["dataFields"] == {"plan": "pro"}


# ═══════════════════════════════════════════════════════════════════════════
# Send APIs
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_send_email(connector):
    route = respx.post(f"{BASE_URL}/email/target").mock(
        return_value=httpx.Response(200, json={"code": "Success"})
    )
    out = await connector.send_email(
        campaign_id=123,
        recipient_email="to@x.com",
        data_fields={"firstName": "Jane"},
    )
    assert out["code"] == "Success"
    body = json.loads(route.calls.last.request.content.decode())
    assert body["campaignId"] == 123
    assert body["recipientEmail"] == "to@x.com"
    assert body["dataFields"] == {"firstName": "Jane"}


@respx.mock
@pytest.mark.asyncio
async def test_send_sms(connector):
    route = respx.post(f"{BASE_URL}/sms/target").mock(
        return_value=httpx.Response(200, json={"code": "Success"})
    )
    out = await connector.send_sms(
        campaign_id=321, recipient_email="sms@x.com"
    )
    assert out["code"] == "Success"
    body = json.loads(route.calls.last.request.content.decode())
    assert body["campaignId"] == 321


@respx.mock
@pytest.mark.asyncio
async def test_send_push_requires_recipient(connector):
    with pytest.raises(ValueError):
        await connector.send_push(campaign_id=9)


@respx.mock
@pytest.mark.asyncio
async def test_send_push_with_user_id(connector):
    route = respx.post(f"{BASE_URL}/push/target").mock(
        return_value=httpx.Response(200, json={"code": "Success"})
    )
    out = await connector.send_push(campaign_id=9, recipient_user_id="u-1")
    assert out["code"] == "Success"
    body = json.loads(route.calls.last.request.content.decode())
    assert body["recipientUserId"] == "u-1"


# ═══════════════════════════════════════════════════════════════════════════
# In-App
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_get_in_app_messages(connector):
    route = respx.get(f"{BASE_URL}/inApp/getMessages").mock(
        return_value=httpx.Response(200, json={"inAppMessages": []})
    )
    out = await connector.get_in_app_messages(email="u@x.com", count=10)
    assert out == {"inAppMessages": []}
    qs = route.calls.last.request.url.params
    assert qs.get("email") == "u@x.com"
    assert qs.get("count") == "10"


# ═══════════════════════════════════════════════════════════════════════════
# Retry behaviour (429 + 5xx)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/lists").mock(
        side_effect=[
            httpx.Response(
                429, json={"msg": "rate limit"}, headers={"Retry-After": "0"}
            ),
            httpx.Response(200, json={"lists": [{"id": 1, "name": "After"}]}),
        ]
    )
    out = await connector.list_lists()
    assert route.call_count == 2
    assert out[0]["name"] == "After"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/lists").mock(
        side_effect=[
            httpx.Response(500, json={"msg": "boom"}),
            httpx.Response(200, json={"lists": []}),
        ]
    )
    out = await connector.list_lists()
    assert route.call_count == 2
    assert out == []


# ═══════════════════════════════════════════════════════════════════════════
# 4xx surfacing
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_iterable_auth_error(connector):
    respx.get(f"{BASE_URL}/lists").mock(
        return_value=httpx.Response(401, json={"msg": "Invalid API key"})
    )
    with pytest.raises(IterableAuthError):
        await connector.list_lists()


@respx.mock
@pytest.mark.asyncio
async def test_bad_request_400_raises_typed_exception(connector):
    respx.post(f"{BASE_URL}/users/update").mock(
        return_value=httpx.Response(400, json={"msg": "missing email"})
    )
    with pytest.raises(IterableBadRequestError):
        await connector.update_user(email="x@y.com", data_fields={})


# ═══════════════════════════════════════════════════════════════════════════
# sync() — ingests templates + lists
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_sync_ingests_templates_and_lists(connector):
    respx.get(f"{BASE_URL}/templates").mock(
        return_value=httpx.Response(
            200,
            json={
                "templates": [
                    {"templateId": 1, "name": "Welcome", "html": "<p>Hi</p>"}
                ]
            },
        )
    )
    respx.get(f"{BASE_URL}/lists").mock(
        return_value=httpx.Response(
            200, json={"lists": [{"id": 10, "name": "VIP"}]}
        )
    )
    result = await connector.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0
