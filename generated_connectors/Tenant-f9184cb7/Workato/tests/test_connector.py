"""Unit tests for WorkatoConnector — respx-mocked, zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import WorkatoConnector
from exceptions import WorkatoAuthError, WorkatoError, WorkatoNotFound

from tests.conftest import (
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_TOKEN,
    TEST_CONFIG,
    WORKATO_BASE,
    WORKATO_BASE_EU,
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
async def test_install_missing_api_token():
    c = WorkatoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer) + region routing
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer_token(connector):
    """Connector must send `Authorization: Bearer <api_token>`."""
    route = respx.get(f"{WORKATO_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "Tester"})
    )
    await connector.health_check()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_API_TOKEN}"


@respx.mock
@pytest.mark.asyncio
async def test_region_eu_targets_eu_base_url():
    """region='eu' must route to https://app.eu.workato.com/api."""
    cfg = dict(TEST_CONFIG)
    cfg["region"] = "eu"
    cfg.pop("base_url", None)
    c = WorkatoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )
    route = respx.get(f"{WORKATO_BASE_EU}/users/me").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    result = await c.health_check()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_health_check_offline(connector):
    respx.get(f"{WORKATO_BASE}/users/me").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.OFFLINE


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_403_health_check_unhealthy(connector):
    respx.get(f"{WORKATO_BASE}/users/me").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{WORKATO_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "Workato Bot"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


# ═══════════════════════════════════════════════════════════════════════════
# Recipes
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_recipes_success(connector):
    payload = {"result": [{"id": 1, "name": "Onboarding"}, {"id": 2, "name": "Sync"}]}
    route = respx.get(f"{WORKATO_BASE}/recipes").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_recipes(page=1, per_page=50, folder_id=42, order="default")
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("page") == "1"
    assert qs.get("per_page") == "50"
    assert qs.get("folder_id") == "42"
    assert qs.get("order") == "default"
    assert result["result"][0]["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_get_recipe_success(connector):
    respx.get(f"{WORKATO_BASE}/recipes/99").mock(
        return_value=httpx.Response(200, json={"id": 99, "name": "R99"})
    )
    result = await connector.get_recipe(99)
    assert result["id"] == 99


@respx.mock
@pytest.mark.asyncio
async def test_get_recipe_not_found(connector):
    respx.get(f"{WORKATO_BASE}/recipes/missing").mock(
        return_value=httpx.Response(404, json={"message": "no such recipe"})
    )
    with pytest.raises(WorkatoNotFound):
        await connector.get_recipe("missing")


@respx.mock
@pytest.mark.asyncio
async def test_start_recipe_success(connector):
    route = respx.put(f"{WORKATO_BASE}/recipes/7/start").mock(
        return_value=httpx.Response(200, json={"running": True})
    )
    result = await connector.start_recipe(7)
    assert route.called
    assert result["running"] is True


@respx.mock
@pytest.mark.asyncio
async def test_stop_recipe_success(connector):
    route = respx.put(f"{WORKATO_BASE}/recipes/7/stop").mock(
        return_value=httpx.Response(200, json={"running": False})
    )
    result = await connector.stop_recipe(7)
    assert route.called
    assert result["running"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Connections
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_connections_success(connector):
    payload = {"result": [{"id": 11, "name": "Salesforce prod", "provider": "salesforce"}]}
    route = respx.get(f"{WORKATO_BASE}/connections").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_connections(page=2, per_page=25)
    qs = route.calls[0].request.url.params
    assert qs.get("page") == "2"
    assert qs.get("per_page") == "25"
    assert result["result"][0]["provider"] == "salesforce"


@respx.mock
@pytest.mark.asyncio
async def test_get_connection_success(connector):
    respx.get(f"{WORKATO_BASE}/connections/11").mock(
        return_value=httpx.Response(200, json={"id": 11, "name": "x"})
    )
    result = await connector.get_connection(11)
    assert result["id"] == 11


@respx.mock
@pytest.mark.asyncio
async def test_create_connection_posts_body(connector):
    route = respx.post(f"{WORKATO_BASE}/connections").mock(
        return_value=httpx.Response(200, json={"id": 999, "name": "new"})
    )
    payload = {"name": "new conn", "provider": "salesforce"}
    result = await connector.create_connection(payload)
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == payload
    assert result["id"] == 999


# ═══════════════════════════════════════════════════════════════════════════
# Folders / Jobs / Lookup tables / Tags / Users / OPA / Customers
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_folders_success(connector):
    route = respx.get(f"{WORKATO_BASE}/folders").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 1, "name": "Default"}]})
    )
    result = await connector.list_folders(parent_id=0)
    qs = route.calls[0].request.url.params
    assert qs.get("parent_id") == "0"
    assert result["result"][0]["name"] == "Default"


@respx.mock
@pytest.mark.asyncio
async def test_list_jobs_recipe_scoped(connector):
    route = respx.get(f"{WORKATO_BASE}/recipes/5/jobs").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 100, "status": "succeeded"}]})
    )
    result = await connector.list_jobs(5, page=1, per_page=10, status="succeeded")
    qs = route.calls[0].request.url.params
    assert qs.get("status") == "succeeded"
    assert route.called
    assert result["result"][0]["id"] == 100


@respx.mock
@pytest.mark.asyncio
async def test_get_job_success(connector):
    respx.get(f"{WORKATO_BASE}/recipes/5/jobs/100").mock(
        return_value=httpx.Response(200, json={"id": 100, "status": "succeeded"})
    )
    result = await connector.get_job(5, 100)
    assert result["status"] == "succeeded"


@respx.mock
@pytest.mark.asyncio
async def test_list_lookup_tables_success(connector):
    respx.get(f"{WORKATO_BASE}/lookup_tables").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 1, "name": "Countries"}]})
    )
    result = await connector.list_lookup_tables()
    assert result["result"][0]["name"] == "Countries"


@respx.mock
@pytest.mark.asyncio
async def test_list_tags_success(connector):
    respx.get(f"{WORKATO_BASE}/tags").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 1, "name": "prod"}]})
    )
    result = await connector.list_tags()
    assert result["result"][0]["name"] == "prod"


@respx.mock
@pytest.mark.asyncio
async def test_list_users_success(connector):
    respx.get(f"{WORKATO_BASE}/users").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 1, "email": "a@b.com"}]})
    )
    result = await connector.list_users()
    assert result["result"][0]["email"] == "a@b.com"


@respx.mock
@pytest.mark.asyncio
async def test_list_on_prem_agents_success(connector):
    respx.get(f"{WORKATO_BASE}/on_prem_agents").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 1, "name": "OPA-1"}]})
    )
    result = await connector.list_on_prem_agents()
    assert result["result"][0]["name"] == "OPA-1"


@respx.mock
@pytest.mark.asyncio
async def test_list_customers_success(connector):
    respx.get(f"{WORKATO_BASE}/managed_users").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 7, "name": "AcmeCo"}]})
    )
    result = await connector.list_customers()
    assert result["result"][0]["name"] == "AcmeCo"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{WORKATO_BASE}/users/me").mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    result = await connector.health_check()
    assert route.call_count == 2
    assert result.health == ConnectorHealth.HEALTHY


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{WORKATO_BASE}/recipes").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"result": []}),
        ]
    )
    result = await connector.list_recipes()
    assert route.call_count == 2
    assert result == {"result": []}


# ═══════════════════════════════════════════════════════════════════════════
# Sync — aggregates recipes + connections + jobs
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_sync_aggregates(connector):
    respx.get(f"{WORKATO_BASE}/recipes").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 1, "name": "R1"}]})
    )
    respx.get(f"{WORKATO_BASE}/connections").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 11, "name": "C1"}]})
    )
    respx.get(f"{WORKATO_BASE}/recipes/1/jobs").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 100, "status": "succeeded"}]})
    )
    result = await connector.sync()
    # 1 recipe + 1 connection + 1 job = 3 docs
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert WorkatoConnector.CONNECTOR_TYPE == "workato"


def test_auth_type_class_attr():
    assert WorkatoConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(WorkatoConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_token" in WorkatoConnector.REQUIRED_CONFIG_KEYS


def test_status_map_class_attr():
    assert 401 in WorkatoConnector._STATUS_MAP
    assert 403 in WorkatoConnector._STATUS_MAP
    assert 429 in WorkatoConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = WorkatoConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = WorkatoConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — tenant-scoped NormalizedDocument id
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_recipe_tenant_scoped_id():
    from helpers.normalizer import normalize_recipe
    raw = {"id": 42, "name": "R", "description": "d", "running": True}
    doc = normalize_recipe(raw, connector_id="conn-x", tenant_id="t-1")
    assert doc.id == "conn-x_42"
    assert doc.source_id == "42"
    assert doc.metadata["kind"] == "workato.recipe"


def test_normalize_connection_tenant_scoped_id():
    from helpers.normalizer import normalize_connection
    raw = {"id": 11, "name": "C", "provider": "salesforce", "authorization_status": "success"}
    doc = normalize_connection(raw, connector_id="conn-x", tenant_id="t-1")
    assert doc.id == "conn-x_11"
    assert doc.metadata["kind"] == "workato.connection"


def test_normalize_job_tenant_scoped_id():
    from helpers.normalizer import normalize_job
    raw = {"id": 100, "status": "succeeded", "recipe_id": 5}
    doc = normalize_job(raw, connector_id="conn-x", tenant_id="t-1")
    assert doc.id == "conn-x_100"
    assert doc.metadata["kind"] == "workato.job"
