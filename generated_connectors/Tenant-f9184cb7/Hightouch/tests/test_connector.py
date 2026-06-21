"""Unit tests for HightouchConnector — respx-mocked, zero real HTTP."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import HightouchConnector
from exceptions import (
    HightouchAuthError,
    HightouchError,
    HightouchNotFoundError,
)

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    SAMPLE_DESTINATION,
    SAMPLE_MODEL,
    SAMPLE_SOURCE,
    SAMPLE_SYNC,
    SAMPLE_SYNC_RUN,
    SAMPLE_WORKSPACE,
    TENANT_ID,
    TEST_API_TOKEN,
    TEST_CONFIG,
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
    cfg = dict(TEST_CONFIG)
    cfg.pop("api_token")
    c = HightouchConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_legacy_api_key_field_still_accepted():
    """The legacy ``api_key`` field should still init the connector for back-compat."""
    cfg = {"api_key": "legacy_key", "base_url": BASE_URL}
    c = HightouchConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )
    assert c.api_token == "legacy_key"
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Auth headers
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_bearer_header_on_every_request(connector):
    route = respx.get(f"{BASE_URL}/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [SAMPLE_WORKSPACE]})
    )
    await connector.list_workspaces()
    assert route.called
    assert (
        route.calls.last.request.headers.get("authorization")
        == f"Bearer {TEST_API_TOKEN}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{BASE_URL}/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [SAMPLE_WORKSPACE]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{BASE_URL}/workspaces").mock(
        return_value=httpx.Response(401, json={"message": "invalid token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_missing_token():
    cfg = dict(TEST_CONFIG)
    cfg.pop("api_token")
    c = HightouchConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Workspaces
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_workspaces(connector):
    respx.get(f"{BASE_URL}/workspaces").mock(
        return_value=httpx.Response(200, json={"workspaces": [SAMPLE_WORKSPACE]})
    )
    result = await connector.list_workspaces()
    assert result["workspaces"][0]["id"] == 42


# ═══════════════════════════════════════════════════════════════════════════
# Sources
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_sources_with_pagination_params(connector):
    route = respx.get(f"{BASE_URL}/sources").mock(
        return_value=httpx.Response(200, json={"sources": [SAMPLE_SOURCE]})
    )
    result = await connector.list_sources(page=2, per_page=10, slug="prod-snowflake")
    assert result["sources"][0]["id"] == 11
    qs = route.calls.last.request.url.params
    assert qs.get("page") == "2"
    assert qs.get("per_page") == "10"
    assert qs.get("slug") == "prod-snowflake"


@respx.mock
@pytest.mark.asyncio
async def test_get_source(connector):
    respx.get(f"{BASE_URL}/sources/11").mock(
        return_value=httpx.Response(200, json=SAMPLE_SOURCE)
    )
    result = await connector.get_source(11)
    assert result["id"] == 11


@respx.mock
@pytest.mark.asyncio
async def test_get_source_not_found(connector):
    respx.get(f"{BASE_URL}/sources/999").mock(
        return_value=httpx.Response(404, json={"message": "no such source"})
    )
    with pytest.raises(HightouchNotFoundError):
        await connector.get_source(999)


# ═══════════════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_models(connector):
    respx.get(f"{BASE_URL}/models").mock(
        return_value=httpx.Response(200, json={"models": [SAMPLE_MODEL]})
    )
    result = await connector.list_models(per_page=20)
    assert result["models"][0]["id"] == 99


@respx.mock
@pytest.mark.asyncio
async def test_get_model(connector):
    respx.get(f"{BASE_URL}/models/99").mock(
        return_value=httpx.Response(200, json=SAMPLE_MODEL)
    )
    result = await connector.get_model(99)
    assert result["primaryKey"] == "user_id"


# ═══════════════════════════════════════════════════════════════════════════
# Destinations
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_destinations(connector):
    respx.get(f"{BASE_URL}/destinations").mock(
        return_value=httpx.Response(
            200,
            json={"destinations": [SAMPLE_DESTINATION]},
        )
    )
    result = await connector.list_destinations(per_page=20)
    assert result["destinations"][0]["id"] == 22


@respx.mock
@pytest.mark.asyncio
async def test_get_destination(connector):
    respx.get(f"{BASE_URL}/destinations/22").mock(
        return_value=httpx.Response(200, json=SAMPLE_DESTINATION)
    )
    result = await connector.get_destination(22)
    assert result["id"] == 22


# ═══════════════════════════════════════════════════════════════════════════
# Syncs
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_syncs_with_filters(connector):
    route = respx.get(f"{BASE_URL}/syncs").mock(
        return_value=httpx.Response(200, json={"syncs": [SAMPLE_SYNC]})
    )
    result = await connector.list_syncs(model_id=99, destination_id=22)
    assert result["syncs"][0]["id"] == 33
    qs = route.calls.last.request.url.params
    assert qs.get("modelId") == "99"
    assert qs.get("destinationId") == "22"


@respx.mock
@pytest.mark.asyncio
async def test_get_sync(connector):
    respx.get(f"{BASE_URL}/syncs/33").mock(
        return_value=httpx.Response(200, json=SAMPLE_SYNC)
    )
    result = await connector.get_sync(33)
    assert result["id"] == 33


@respx.mock
@pytest.mark.asyncio
async def test_create_sync(connector):
    route = respx.post(f"{BASE_URL}/syncs").mock(
        return_value=httpx.Response(201, json={"id": 555, "slug": "new-sync"})
    )
    payload = {"slug": "new-sync", "modelId": 99, "destinationId": 22}
    result = await connector.create_sync(payload)
    assert result["id"] == 555
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["slug"] == "new-sync"
    assert body["modelId"] == 99


@respx.mock
@pytest.mark.asyncio
async def test_run_sync_default(connector):
    route = respx.post(f"{BASE_URL}/syncs/33/trigger").mock(
        return_value=httpx.Response(200, json={"syncRunId": 9001})
    )
    result = await connector.run_sync(33)
    assert result["syncRunId"] == 9001
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["fullResync"] is False


@respx.mock
@pytest.mark.asyncio
async def test_run_sync_full_resync(connector):
    route = respx.post(f"{BASE_URL}/syncs/33/trigger").mock(
        return_value=httpx.Response(200, json={"syncRunId": 9002})
    )
    await connector.run_sync(33, full_resync=True)
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["fullResync"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Sync runs
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_sync_runs(connector):
    respx.get(f"{BASE_URL}/syncs/33/runs").mock(
        return_value=httpx.Response(200, json={"runs": [SAMPLE_SYNC_RUN]})
    )
    result = await connector.list_sync_runs(33, page=1, per_page=10)
    assert result["runs"][0]["id"] == 7777


@respx.mock
@pytest.mark.asyncio
async def test_get_sync_run(connector):
    respx.get(f"{BASE_URL}/syncs/33/runs/7777").mock(
        return_value=httpx.Response(200, json=SAMPLE_SYNC_RUN)
    )
    result = await connector.get_sync_run(33, 7777)
    assert result["status"] == "success"


# ═══════════════════════════════════════════════════════════════════════════
# Sequences
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_sequences(connector):
    respx.get(f"{BASE_URL}/sequences").mock(
        return_value=httpx.Response(
            200,
            json={"sequences": [{"id": 1, "name": "Daily"}]},
        )
    )
    result = await connector.list_sequences()
    assert result["sequences"][0]["id"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Events
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_send_event(connector):
    route = respx.post(f"{BASE_URL}/events").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    event = {"userId": "u-1", "event": "Signed Up"}
    result = await connector.send_event(event)
    assert result == {"status": "OK"}
    body = _json.loads(route.calls.last.request.read().decode())
    assert body == event


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/workspaces").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow down"}),
            httpx.Response(200, json={"workspaces": [SAMPLE_WORKSPACE]}),
        ]
    )
    result = await connector.list_workspaces()
    assert route.call_count == 2
    assert result["workspaces"][0]["id"] == 42


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/syncs").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"syncs": []}),
        ]
    )
    result = await connector.list_syncs()
    assert route.call_count == 2
    assert result == {"syncs": []}


# ═══════════════════════════════════════════════════════════════════════════
# Auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_sources_auth_error(connector):
    respx.get(f"{BASE_URL}/sources").mock(
        return_value=httpx.Response(401, json={"message": "invalid api_token"})
    )
    with pytest.raises(HightouchAuthError):
        await connector.list_sources()


@pytest.mark.asyncio
async def test_missing_token_raises_on_call():
    """A call without an api_token must raise HightouchAuthError from the HTTP layer."""
    cfg = dict(TEST_CONFIG)
    cfg.pop("api_token")
    c = HightouchConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )
    with pytest.raises(HightouchAuthError):
        await c.list_sources()


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert HightouchConnector.CONNECTOR_TYPE == "hightouch"


def test_auth_type_class_attr():
    assert HightouchConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(HightouchConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_token" in HightouchConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(HightouchConnector, "_STATUS_MAP")
    assert 401 in HightouchConnector._STATUS_MAP
    assert 403 in HightouchConnector._STATUS_MAP
    assert 429 in HightouchConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = HightouchConnector(
        tenant_id="t-A",
        connector_id="conn-1",
        config=dict(TEST_CONFIG),
    )
    c2 = HightouchConnector(
        tenant_id="t-B",
        connector_id="conn-2",
        config=dict(TEST_CONFIG),
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizers — id format (tenant_id_{source_id})
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_source_id_format():
    from helpers.normalizer import normalize_source

    doc = normalize_source(SAMPLE_SOURCE, "conn-1", TENANT_ID)
    assert doc.id == f"{TENANT_ID}_11"
    assert doc.source_id == "11"
    assert doc.metadata["type"] == "snowflake"
    assert doc.metadata["kind"] == "hightouch.source"


def test_normalize_model_id_format():
    from helpers.normalizer import normalize_model

    doc = normalize_model(SAMPLE_MODEL, "conn-1", TENANT_ID)
    assert doc.id == f"{TENANT_ID}_99"
    assert doc.metadata["primaryKey"] == "user_id"
    assert doc.metadata["kind"] == "hightouch.model"


def test_normalize_destination_id_format():
    from helpers.normalizer import normalize_destination

    doc = normalize_destination(SAMPLE_DESTINATION, "conn-1", TENANT_ID)
    assert doc.id == f"{TENANT_ID}_22"
    assert doc.metadata["kind"] == "hightouch.destination"


def test_normalize_sync_id_format():
    from helpers.normalizer import normalize_sync

    doc = normalize_sync(SAMPLE_SYNC, "conn-1", TENANT_ID)
    assert doc.id == f"{TENANT_ID}_33"
    assert doc.metadata["modelId"] == 99
    assert doc.metadata["destinationId"] == 22
    assert doc.metadata["kind"] == "hightouch.sync"


# ═══════════════════════════════════════════════════════════════════════════
# Sync orchestration
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_sync_orchestrates_all_inventory(connector):
    respx.get(f"{BASE_URL}/sources").mock(
        return_value=httpx.Response(200, json={"sources": [SAMPLE_SOURCE]})
    )
    respx.get(f"{BASE_URL}/models").mock(
        return_value=httpx.Response(200, json={"models": [SAMPLE_MODEL]})
    )
    respx.get(f"{BASE_URL}/destinations").mock(
        return_value=httpx.Response(
            200, json={"destinations": [SAMPLE_DESTINATION]}
        )
    )
    respx.get(f"{BASE_URL}/syncs").mock(
        return_value=httpx.Response(200, json={"syncs": [SAMPLE_SYNC]})
    )
    result = await connector.sync()
    assert result.documents_found == 4
    assert result.documents_synced == 4
    assert result.documents_failed == 0
