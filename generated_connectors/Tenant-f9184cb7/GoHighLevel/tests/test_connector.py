"""Unit tests for GoHighLevelConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import GoHighLevelConnector
from exceptions import (
    GoHighLevelAuthError,
    GoHighLevelError,
    GoHighLevelNotFound,
)

from tests.conftest import (
    CONNECTOR_ID,
    GHL_BASE,
    TENANT_ID,
    TEST_API_KEY,
    TEST_API_VERSION,
    TEST_CONFIG,
    TEST_LOCATION_ID,
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
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_authorize_returns_token_info(connector):
    token = await connector.authorize()
    assert token.access_token == TEST_API_KEY
    assert token.token_type == "api_key"


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer + Version) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_bearer_and_version(connector):
    """Connector must send api_key as 'Bearer <key>' + mandatory Version header."""
    route = respx.get(f"{GHL_BASE}/locations/{TEST_LOCATION_ID}").mock(
        return_value=httpx.Response(200, json={"location": {"id": TEST_LOCATION_ID}})
    )
    await connector.get_location(TEST_LOCATION_ID)
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_API_KEY}"
    assert route.calls[0].request.headers.get("version") == TEST_API_VERSION


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_ghl_auth_error(connector):
    respx.get(f"{GHL_BASE}/locations/search").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"})
    )
    # use a fresh connector with no location_id so it hits /locations/search
    connector.location_id = ""
    with pytest.raises(GoHighLevelAuthError):
        await connector.list_locations()


# ═══════════════════════════════════════════════════════════════════════════
# health_check
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy_with_location(connector):
    respx.get(f"{GHL_BASE}/locations/{TEST_LOCATION_ID}").mock(
        return_value=httpx.Response(200, json={"location": {"id": TEST_LOCATION_ID}})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy_without_location(connector):
    connector.location_id = ""
    respx.get(f"{GHL_BASE}/locations/search").mock(
        return_value=httpx.Response(200, json={"locations": [{"id": "loc1"}]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_offline(connector):
    respx.get(f"{GHL_BASE}/locations/{TEST_LOCATION_ID}").mock(
        return_value=httpx.Response(401, json={"message": "Invalid key"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_unhealthy(connector):
    respx.get(f"{GHL_BASE}/locations/{TEST_LOCATION_ID}").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Locations
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_locations_success(connector):
    payload = {"locations": [{"id": "L1", "name": "Loc One"}]}
    route = respx.get(f"{GHL_BASE}/locations/search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_locations(limit=5, skip=0)
    assert route.called
    assert result["locations"][0]["id"] == "L1"
    qs = route.calls[0].request.url.params
    assert qs.get("limit") == "5"
    assert qs.get("skip") == "0"


@respx.mock
@pytest.mark.asyncio
async def test_get_location_not_found(connector):
    respx.get(f"{GHL_BASE}/locations/missing").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    with pytest.raises(GoHighLevelNotFound):
        await connector.get_location("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Contacts
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_passes_query_params(connector):
    route = respx.get(f"{GHL_BASE}/contacts/").mock(
        return_value=httpx.Response(200, json={"contacts": [{"id": "c1"}]})
    )
    result = await connector.list_contacts(
        location_id=TEST_LOCATION_ID,
        limit=50,
        page=2,
        query="ada",
    )
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("locationId") == TEST_LOCATION_ID
    assert qs.get("limit") == "50"
    assert qs.get("page") == "2"
    assert qs.get("query") == "ada"
    assert result["contacts"][0]["id"] == "c1"


@respx.mock
@pytest.mark.asyncio
async def test_get_contact_success(connector):
    cid = "c42"
    respx.get(f"{GHL_BASE}/contacts/{cid}").mock(
        return_value=httpx.Response(200, json={"contact": {"id": cid, "email": "x@y.com"}})
    )
    result = await connector.get_contact(cid)
    assert result["contact"]["id"] == cid


@respx.mock
@pytest.mark.asyncio
async def test_create_contact_posts_payload_with_default_location(connector):
    route = respx.post(f"{GHL_BASE}/contacts/").mock(
        return_value=httpx.Response(200, json={"contact": {"id": "new-c"}})
    )
    payload = {"firstName": "Ada", "email": "ada@ex.com"}
    result = await connector.create_contact(payload)
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    # default location_id injected
    assert body["firstName"] == "Ada"
    assert body["email"] == "ada@ex.com"
    assert body["locationId"] == TEST_LOCATION_ID
    assert result["contact"]["id"] == "new-c"


@respx.mock
@pytest.mark.asyncio
async def test_update_contact_puts_payload(connector):
    cid = "c-edit"
    route = respx.put(f"{GHL_BASE}/contacts/{cid}").mock(
        return_value=httpx.Response(200, json={"contact": {"id": cid, "firstName": "Bea"}})
    )
    result = await connector.update_contact(cid, {"firstName": "Bea"})
    assert route.called
    assert result["contact"]["firstName"] == "Bea"


@respx.mock
@pytest.mark.asyncio
async def test_delete_contact_success(connector):
    cid = "c-bye"
    respx.delete(f"{GHL_BASE}/contacts/{cid}").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_contact(cid)
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Opportunities
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_opportunities_passes_filters(connector):
    route = respx.get(f"{GHL_BASE}/opportunities/search").mock(
        return_value=httpx.Response(200, json={"opportunities": [{"id": "o1"}]})
    )
    result = await connector.list_opportunities(
        location_id=TEST_LOCATION_ID,
        pipeline_id="pl-1",
        limit=25,
        page=1,
    )
    qs = route.calls[0].request.url.params
    assert qs.get("location_id") == TEST_LOCATION_ID
    assert qs.get("pipeline_id") == "pl-1"
    assert qs.get("limit") == "25"
    assert result["opportunities"][0]["id"] == "o1"


@respx.mock
@pytest.mark.asyncio
async def test_get_opportunity_success(connector):
    oid = "o-9"
    respx.get(f"{GHL_BASE}/opportunities/{oid}").mock(
        return_value=httpx.Response(200, json={"opportunity": {"id": oid, "name": "Deal"}})
    )
    result = await connector.get_opportunity(oid)
    assert result["opportunity"]["id"] == oid


@respx.mock
@pytest.mark.asyncio
async def test_create_opportunity_posts_payload(connector):
    route = respx.post(f"{GHL_BASE}/opportunities/").mock(
        return_value=httpx.Response(200, json={"opportunity": {"id": "new-o"}})
    )
    payload = {"name": "Big Deal", "pipelineId": "pl-1", "pipelineStageId": "st-1"}
    result = await connector.create_opportunity(payload)
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["name"] == "Big Deal"
    assert body["pipelineId"] == "pl-1"
    assert body["locationId"] == TEST_LOCATION_ID
    assert result["opportunity"]["id"] == "new-o"


@respx.mock
@pytest.mark.asyncio
async def test_update_opportunity_puts_payload(connector):
    oid = "o-edit"
    route = respx.put(f"{GHL_BASE}/opportunities/{oid}").mock(
        return_value=httpx.Response(200, json={"opportunity": {"id": oid, "status": "won"}})
    )
    result = await connector.update_opportunity(oid, {"status": "won"})
    assert route.called
    assert result["opportunity"]["status"] == "won"


# ═══════════════════════════════════════════════════════════════════════════
# Conversations
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_conversations_success(connector):
    route = respx.get(f"{GHL_BASE}/conversations/search").mock(
        return_value=httpx.Response(200, json={"conversations": [{"id": "conv1"}]})
    )
    result = await connector.list_conversations(
        location_id=TEST_LOCATION_ID, contact_id="c-1", limit=10
    )
    qs = route.calls[0].request.url.params
    assert qs.get("locationId") == TEST_LOCATION_ID
    assert qs.get("contactId") == "c-1"
    assert result["conversations"][0]["id"] == "conv1"


@respx.mock
@pytest.mark.asyncio
async def test_get_conversation_success(connector):
    cv = "conv-1"
    respx.get(f"{GHL_BASE}/conversations/{cv}").mock(
        return_value=httpx.Response(200, json={"conversation": {"id": cv}})
    )
    result = await connector.get_conversation(cv)
    assert result["conversation"]["id"] == cv


@respx.mock
@pytest.mark.asyncio
async def test_send_message_posts_to_messages(connector):
    route = respx.post(f"{GHL_BASE}/conversations/messages").mock(
        return_value=httpx.Response(200, json={"messageId": "m1"})
    )
    payload = {"type": "SMS", "message": "Hi", "contactId": "c-1"}
    result = await connector.send_message("conv-1", payload)
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["conversationId"] == "conv-1"
    assert body["type"] == "SMS"
    assert body["message"] == "Hi"
    assert result["messageId"] == "m1"


# ═══════════════════════════════════════════════════════════════════════════
# Calendars · Pipelines · Users · Campaigns
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_calendars_success(connector):
    respx.get(f"{GHL_BASE}/calendars/").mock(
        return_value=httpx.Response(200, json={"calendars": [{"id": "cal1"}]})
    )
    result = await connector.list_calendars(location_id=TEST_LOCATION_ID)
    assert result["calendars"][0]["id"] == "cal1"


@respx.mock
@pytest.mark.asyncio
async def test_list_pipelines_success(connector):
    respx.get(f"{GHL_BASE}/opportunities/pipelines").mock(
        return_value=httpx.Response(200, json={"pipelines": [{"id": "pl1"}]})
    )
    result = await connector.list_pipelines(location_id=TEST_LOCATION_ID)
    assert result["pipelines"][0]["id"] == "pl1"


@respx.mock
@pytest.mark.asyncio
async def test_list_users_success(connector):
    respx.get(f"{GHL_BASE}/users/").mock(
        return_value=httpx.Response(200, json={"users": [{"id": "u1"}]})
    )
    result = await connector.list_users(location_id=TEST_LOCATION_ID)
    assert result["users"][0]["id"] == "u1"


@respx.mock
@pytest.mark.asyncio
async def test_list_campaigns_success(connector):
    respx.get(f"{GHL_BASE}/campaigns/").mock(
        return_value=httpx.Response(200, json={"campaigns": [{"id": "cmp1"}]})
    )
    result = await connector.list_campaigns(location_id=TEST_LOCATION_ID)
    assert result["campaigns"][0]["id"] == "cmp1"


@respx.mock
@pytest.mark.asyncio
async def test_list_custom_fields_success(connector):
    respx.get(f"{GHL_BASE}/locations/{TEST_LOCATION_ID}/customFields").mock(
        return_value=httpx.Response(200, json={"customFields": [{"id": "cf1"}]})
    )
    result = await connector.list_custom_fields(TEST_LOCATION_ID)
    assert result["customFields"][0]["id"] == "cf1"


@respx.mock
@pytest.mark.asyncio
async def test_list_tags_success(connector):
    respx.get(f"{GHL_BASE}/locations/{TEST_LOCATION_ID}/tags").mock(
        return_value=httpx.Response(200, json={"tags": [{"id": "t1", "name": "vip"}]})
    )
    result = await connector.list_tags(TEST_LOCATION_ID)
    assert result["tags"][0]["name"] == "vip"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 500 — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{GHL_BASE}/locations/{TEST_LOCATION_ID}").mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(200, json={"location": {"id": "after-retry"}}),
        ]
    )
    result = await connector.get_location(TEST_LOCATION_ID)
    assert route.call_count == 2
    assert result["location"]["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{GHL_BASE}/locations/search").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"locations": []}),
        ]
    )
    connector.location_id = ""
    result = await connector.list_locations()
    assert route.call_count == 2
    assert result == {"locations": []}


# ═══════════════════════════════════════════════════════════════════════════
# Sync — aggregates contacts + opportunities + conversations
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_sync_aggregates_all_surfaces(connector):
    respx.get(f"{GHL_BASE}/contacts/").mock(
        return_value=httpx.Response(200, json={"contacts": [{"id": "c1", "dateAdded": "2026-01-01T00:00:00Z"}]})
    )
    respx.get(f"{GHL_BASE}/opportunities/search").mock(
        return_value=httpx.Response(200, json={"opportunities": [{"id": "o1", "name": "x"}]})
    )
    respx.get(f"{GHL_BASE}/conversations/search").mock(
        return_value=httpx.Response(200, json={"conversations": [{"id": "v1", "contactId": "c1"}]})
    )
    result = await connector.sync()
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert GoHighLevelConnector.CONNECTOR_TYPE == "gohighlevel"


def test_auth_type_class_attr():
    assert GoHighLevelConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(GoHighLevelConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in GoHighLevelConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = GoHighLevelConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = GoHighLevelConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
