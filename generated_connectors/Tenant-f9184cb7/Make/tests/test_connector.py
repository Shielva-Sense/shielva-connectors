"""Unit tests for MakeConnector — respx-mocked, zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import MakeConnector
from exceptions import MakeAuthError, MakeNotFound

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_TOKEN,
    TEST_CONFIG,
    TEST_ORG_ID,
    TEST_TEAM_ID,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_install_success(connector):
    respx.get(f"{BASE_URL}/users/me").mock(
        return_value=httpx.Response(200, json={"id": 7, "name": "Tester"})
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_token():
    cfg = dict(TEST_CONFIG)
    cfg["api_token"] = ""
    c = MakeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_error(connector):
    respx.get(f"{BASE_URL}/users/me").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Token <key>) + auth error paths
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_authorization_header_is_token_scheme(connector):
    """Make wants 'Token <api_token>' — NOT 'Bearer <api_token>'."""
    route = respx.get(f"{BASE_URL}/users/me").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    await connector.health_check()
    assert route.called
    auth = route.calls.last.request.headers.get("Authorization", "")
    assert auth == f"Token {TEST_API_TOKEN}"
    assert not auth.lower().startswith("bearer ")


@pytest.mark.asyncio
@respx.mock
async def test_auth_error_401_raises(connector):
    respx.get(f"{BASE_URL}/users/me").mock(
        return_value=httpx.Response(401, json={"message": "Bad token"})
    )
    with pytest.raises(MakeAuthError):
        await connector.http_client.get("/users/me")


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{BASE_URL}/users/me").mock(
        return_value=httpx.Response(200, json={"id": 7})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error(connector):
    respx.get(f"{BASE_URL}/users/me").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_current_user(connector):
    respx.get(f"{BASE_URL}/users/me").mock(
        return_value=httpx.Response(200, json={"id": 7, "email": "me@example.com"})
    )
    result = await connector.get_current_user()
    assert result["id"] == 7


@pytest.mark.asyncio
@respx.mock
async def test_list_users(connector):
    route = respx.get(f"{BASE_URL}/users").mock(
        return_value=httpx.Response(
            200, json={"users": [{"id": 1, "email": "u1@example.com"}]}
        )
    )
    result = await connector.list_users(organization_id=TEST_ORG_ID, page=1, pageSize=10)
    assert route.called
    url = str(route.calls.last.request.url)
    assert f"organizationId={TEST_ORG_ID}" in url
    assert "page=1" in url
    assert "pageSize=10" in url
    assert result["users"][0]["id"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Organizations
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_organizations(connector):
    respx.get(f"{BASE_URL}/organizations").mock(
        return_value=httpx.Response(
            200, json={"organizations": [{"id": 1, "name": "Acme"}]}
        )
    )
    result = await connector.list_organizations()
    assert result["organizations"][0]["name"] == "Acme"


@pytest.mark.asyncio
@respx.mock
async def test_get_organization(connector):
    respx.get(f"{BASE_URL}/organizations/{TEST_ORG_ID}").mock(
        return_value=httpx.Response(200, json={"id": TEST_ORG_ID, "name": "Acme"})
    )
    result = await connector.get_organization(organization_id=TEST_ORG_ID)
    assert result["id"] == TEST_ORG_ID


# ═══════════════════════════════════════════════════════════════════════════
# Teams
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_teams(connector):
    route = respx.get(f"{BASE_URL}/teams").mock(
        return_value=httpx.Response(
            200, json={"teams": [{"id": 11, "name": "Ops", "organizationId": TEST_ORG_ID}]}
        )
    )
    result = await connector.list_teams(organization_id=TEST_ORG_ID)
    assert route.called
    assert f"organizationId={TEST_ORG_ID}" in str(route.calls.last.request.url)
    assert result["teams"][0]["id"] == 11


@pytest.mark.asyncio
@respx.mock
async def test_get_team(connector):
    respx.get(f"{BASE_URL}/teams/{TEST_TEAM_ID}").mock(
        return_value=httpx.Response(200, json={"id": TEST_TEAM_ID, "name": "Ops"})
    )
    result = await connector.get_team(team_id=TEST_TEAM_ID)
    assert result["id"] == TEST_TEAM_ID


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_scenarios(connector):
    route = respx.get(f"{BASE_URL}/scenarios").mock(
        return_value=httpx.Response(
            200,
            json={
                "scenarios": [
                    {"id": 42, "name": "Sync orders", "teamId": TEST_TEAM_ID}
                ]
            },
        )
    )
    result = await connector.list_scenarios(team_id=TEST_TEAM_ID, page=2, pageSize=25)
    assert route.called
    url = str(route.calls.last.request.url)
    assert f"teamId={TEST_TEAM_ID}" in url
    assert "page=2" in url
    assert "pageSize=25" in url
    assert result["scenarios"][0]["id"] == 42


@pytest.mark.asyncio
@respx.mock
async def test_get_scenario(connector):
    respx.get(f"{BASE_URL}/scenarios/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "name": "Sync orders"})
    )
    result = await connector.get_scenario(scenario_id=42)
    assert result["id"] == 42


@pytest.mark.asyncio
@respx.mock
async def test_get_scenario_not_found(connector):
    respx.get(f"{BASE_URL}/scenarios/999").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )
    with pytest.raises(MakeNotFound):
        await connector.get_scenario(scenario_id=999)


@pytest.mark.asyncio
@respx.mock
async def test_create_scenario_posts_blueprint(connector):
    route = respx.post(f"{BASE_URL}/scenarios").mock(
        return_value=httpx.Response(201, json={"id": 7, "name": "New"})
    )
    result = await connector.create_scenario(
        team_id=TEST_TEAM_ID,
        name="New",
        blueprint={"modules": []},
        scheduling={"type": "indefinitely"},
    )
    assert route.called
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["teamId"] == TEST_TEAM_ID
    assert body["name"] == "New"
    assert body["blueprint"] == {"modules": []}
    assert body["scheduling"] == {"type": "indefinitely"}
    assert result["id"] == 7


@pytest.mark.asyncio
@respx.mock
async def test_update_scenario_patches_fields(connector):
    route = respx.patch(f"{BASE_URL}/scenarios/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "name": "Renamed"})
    )
    result = await connector.update_scenario(scenario_id=42, fields={"name": "Renamed"})
    assert route.called
    body = _json.loads(route.calls.last.request.read().decode())
    assert body == {"name": "Renamed"}
    assert result["name"] == "Renamed"


@pytest.mark.asyncio
@respx.mock
async def test_delete_scenario(connector):
    route = respx.delete(f"{BASE_URL}/scenarios/42").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_scenario(scenario_id=42)
    assert route.called
    assert result == {}


@pytest.mark.asyncio
@respx.mock
async def test_run_scenario(connector):
    route = respx.post(f"{BASE_URL}/scenarios/42/run").mock(
        return_value=httpx.Response(200, json={"executionId": "exec-1"})
    )
    result = await connector.run_scenario(scenario_id=42, body={"foo": "bar"})
    assert route.called
    assert result["executionId"] == "exec-1"


@pytest.mark.asyncio
@respx.mock
async def test_start_scenario(connector):
    route = respx.post(f"{BASE_URL}/scenarios/42/start").mock(
        return_value=httpx.Response(200, json={"isActive": True})
    )
    result = await connector.start_scenario(scenario_id=42)
    assert route.called
    assert result["isActive"] is True


@pytest.mark.asyncio
@respx.mock
async def test_stop_scenario(connector):
    route = respx.post(f"{BASE_URL}/scenarios/42/stop").mock(
        return_value=httpx.Response(200, json={"isActive": False})
    )
    result = await connector.stop_scenario(scenario_id=42)
    assert route.called
    assert result["isActive"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Executions
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_executions(connector):
    route = respx.get(f"{BASE_URL}/executions").mock(
        return_value=httpx.Response(
            200, json={"executions": [{"id": "e1", "scenarioId": 42}]}
        )
    )
    result = await connector.list_executions(scenario_id=42, page=1, pageSize=10)
    assert route.called
    url = str(route.calls.last.request.url)
    assert "scenarioId=42" in url
    assert "page=1" in url
    assert "pageSize=10" in url
    assert result["executions"][0]["id"] == "e1"


@pytest.mark.asyncio
@respx.mock
async def test_get_execution(connector):
    respx.get(f"{BASE_URL}/executions/e1").mock(
        return_value=httpx.Response(200, json={"id": "e1", "status": "success"})
    )
    result = await connector.get_execution(execution_id="e1")
    assert result["status"] == "success"


# ═══════════════════════════════════════════════════════════════════════════
# Connections
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_connections(connector):
    route = respx.get(f"{BASE_URL}/connections").mock(
        return_value=httpx.Response(
            200, json={"connections": [{"id": 1, "name": "Gmail"}]}
        )
    )
    result = await connector.list_connections(team_id=TEST_TEAM_ID)
    assert route.called
    assert f"teamId={TEST_TEAM_ID}" in str(route.calls.last.request.url)
    assert result["connections"][0]["name"] == "Gmail"


@pytest.mark.asyncio
@respx.mock
async def test_get_connection(connector):
    respx.get(f"{BASE_URL}/connections/5").mock(
        return_value=httpx.Response(200, json={"id": 5})
    )
    result = await connector.get_connection(connection_id=5)
    assert result["id"] == 5


# ═══════════════════════════════════════════════════════════════════════════
# Hooks
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_hooks(connector):
    route = respx.get(f"{BASE_URL}/hooks").mock(
        return_value=httpx.Response(
            200,
            json={"hooks": [{"id": 7, "name": "Webhook A", "teamId": TEST_TEAM_ID}]},
        )
    )
    result = await connector.list_hooks(team_id=TEST_TEAM_ID)
    assert route.called
    assert f"teamId={TEST_TEAM_ID}" in str(route.calls.last.request.url)
    assert result["hooks"][0]["id"] == 7


@pytest.mark.asyncio
@respx.mock
async def test_get_hook(connector):
    respx.get(f"{BASE_URL}/hooks/9").mock(
        return_value=httpx.Response(200, json={"id": 9})
    )
    result = await connector.get_hook(hook_id=9)
    assert result["id"] == 9


@pytest.mark.asyncio
@respx.mock
async def test_create_hook(connector):
    route = respx.post(f"{BASE_URL}/hooks").mock(
        return_value=httpx.Response(
            201, json={"id": 9, "name": "Order webhook", "typeName": "webhook"}
        )
    )
    result = await connector.create_hook(
        team_id=TEST_TEAM_ID, name="Order webhook", type_name="webhook"
    )
    assert route.called
    body = _json.loads(route.calls.last.request.read().decode())
    assert body == {
        "teamId": TEST_TEAM_ID,
        "name": "Order webhook",
        "typeName": "webhook",
    }
    assert result["id"] == 9


@pytest.mark.asyncio
@respx.mock
async def test_delete_hook(connector):
    route = respx.delete(f"{BASE_URL}/hooks/9").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_hook(hook_id=9)
    assert route.called
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Data Stores
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_data_stores(connector):
    route = respx.get(f"{BASE_URL}/data-stores").mock(
        return_value=httpx.Response(
            200, json={"dataStores": [{"id": 1, "name": "Cache"}]}
        )
    )
    result = await connector.list_data_stores(team_id=TEST_TEAM_ID, page=1, pageSize=20)
    assert route.called
    url = str(route.calls.last.request.url)
    assert f"teamId={TEST_TEAM_ID}" in url
    assert result["dataStores"][0]["name"] == "Cache"


@pytest.mark.asyncio
@respx.mock
async def test_get_data_store(connector):
    respx.get(f"{BASE_URL}/data-stores/3").mock(
        return_value=httpx.Response(200, json={"id": 3})
    )
    result = await connector.get_data_store(data_store_id=3)
    assert result["id"] == 3


# ═══════════════════════════════════════════════════════════════════════════
# Templates
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_templates(connector):
    route = respx.get(f"{BASE_URL}/templates").mock(
        return_value=httpx.Response(
            200, json={"templates": [{"id": 1, "name": "Shopify→Slack"}]}
        )
    )
    result = await connector.list_templates(team_id=TEST_TEAM_ID, page=1, pageSize=10)
    assert route.called
    assert f"teamId={TEST_TEAM_ID}" in str(route.calls.last.request.url)
    assert result["templates"][0]["id"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Devices
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_devices(connector):
    route = respx.get(f"{BASE_URL}/devices").mock(
        return_value=httpx.Response(
            200, json={"devices": [{"id": 1, "name": "iPhone"}]}
        )
    )
    result = await connector.list_devices(team_id=TEST_TEAM_ID)
    assert route.called
    assert f"teamId={TEST_TEAM_ID}" in str(route.calls.last.request.url)
    assert result["devices"][0]["name"] == "iPhone"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 + 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once with Retry-After: 0, then 200 — connector must retry."""
    responses = [
        httpx.Response(429, json={"message": "slow down"}, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"organizations": [{"id": 1}]}),
    ]

    def _side_effect(request):
        return responses.pop(0)

    respx.get(f"{BASE_URL}/organizations").mock(side_effect=_side_effect)

    result = await connector.list_organizations()
    assert result["organizations"][0]["id"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    responses = [
        httpx.Response(500, json={"message": "boom"}),
        httpx.Response(200, json={"organizations": []}),
    ]

    def _side_effect(request):
        return responses.pop(0)

    respx.get(f"{BASE_URL}/organizations").mock(side_effect=_side_effect)
    result = await connector.list_organizations()
    assert result == {"organizations": []}


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity / multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_is_make():
    assert MakeConnector.CONNECTOR_TYPE == "make"


def test_auth_type_is_api_key():
    assert MakeConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(MakeConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_token" in MakeConnector.REQUIRED_CONFIG_KEYS
    assert "zone" in MakeConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(MakeConnector, "_STATUS_MAP")
    assert 401 in MakeConnector._STATUS_MAP
    assert 403 in MakeConnector._STATUS_MAP
    assert 429 in MakeConnector._STATUS_MAP


def test_independent_instances_per_tenant():
    c1 = MakeConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = MakeConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


@pytest.mark.asyncio
async def test_authorize_records_token(connector):
    info = await connector.authorize(auth_code="brand-new-token")
    assert info.access_token == "brand-new-token"
    assert info.token_type == "Token"
    assert info.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# Zone → base URL builder
# ═══════════════════════════════════════════════════════════════════════════

def test_zone_builds_correct_base_url():
    from helpers.utils import build_base_url
    assert build_base_url("eu1") == "https://eu1.make.com/api/v2"
    assert build_base_url("us1") == "https://us1.make.com/api/v2"
    assert build_base_url("us2") == "https://us2.make.com/api/v2"


def test_us1_zone_connector_uses_us1_host():
    cfg = dict(TEST_CONFIG)
    cfg["zone"] = "us1"
    cfg.pop("base_url", None)
    c = MakeConnector(tenant_id="t", connector_id="c", config=cfg)
    assert c.base_url == "https://us1.make.com/api/v2"
