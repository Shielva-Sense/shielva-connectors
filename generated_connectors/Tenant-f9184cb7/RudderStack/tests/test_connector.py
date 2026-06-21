"""Unit tests for RudderstackConnector — respx-mocked, zero real HTTP."""
import base64
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import RudderstackConnector
from exceptions import (
    RudderstackAuthError,
    RudderstackError,
    RudderstackNotFoundError,
)

from tests.conftest import (
    CONNECTOR_ID,
    CONTROL_PLANE,
    DATA_PLANE,
    TENANT_ID,
    TEST_CONFIG,
    TEST_PAT,
    TEST_WRITE_KEY,
)


def _expected_basic_header(write_key: str) -> str:
    token = base64.b64encode(f"{write_key}:".encode("ascii")).decode("ascii")
    return f"Basic {token}"


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
async def test_install_missing_write_key():
    cfg = dict(TEST_CONFIG)
    cfg.pop("write_key")
    c = RudderstackConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_pat_optional():
    """PAT (access_token) is optional — install should still succeed."""
    cfg = dict(TEST_CONFIG)
    cfg.pop("access_token")
    c = RudderstackConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shapes (Bearer for control, Basic for data)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_control_plane_uses_bearer_pat(connector):
    route = respx.get(f"{CONTROL_PLANE}/sources").mock(
        return_value=httpx.Response(200, json={"sources": []})
    )
    await connector.list_sources(limit=1)
    assert route.called
    assert route.calls.last.request.headers.get("authorization") == f"Bearer {TEST_PAT}"


@respx.mock
@pytest.mark.asyncio
async def test_data_plane_uses_basic_write_key(connector):
    route = respx.post(f"{DATA_PLANE}/v1/track").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    await connector.track_event(user_id="u-1", event="Signed Up")
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["authorization"] == _expected_basic_header(TEST_WRITE_KEY)


@respx.mock
@pytest.mark.asyncio
async def test_data_plane_write_key_override(connector):
    route = respx.post(f"{DATA_PLANE}/v1/track").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    await connector.track_event(
        user_id="u-1",
        event="Clicked",
        write_key="wk_override_999",
    )
    sent = route.calls.last.request
    assert sent.headers["authorization"] == _expected_basic_header("wk_override_999")


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{CONTROL_PLANE}/sources").mock(
        return_value=httpx.Response(200, json={"sources": [{"id": "s1"}]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{CONTROL_PLANE}/sources").mock(
        return_value=httpx.Response(401, json={"message": "invalid token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_no_pat_but_write_key():
    """No PAT → still HEALTHY if write_key is present (data plane available)."""
    cfg = dict(TEST_CONFIG)
    cfg.pop("access_token")
    c = RudderstackConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )
    result = await c.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


# ═══════════════════════════════════════════════════════════════════════════
# Workspaces
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_workspaces(connector):
    respx.get(f"{CONTROL_PLANE}/workspaces").mock(
        return_value=httpx.Response(
            200,
            json={"workspaces": [{"id": "ws_1", "name": "Main"}]},
        )
    )
    result = await connector.list_workspaces()
    assert result["workspaces"][0]["id"] == "ws_1"


# ═══════════════════════════════════════════════════════════════════════════
# Sources
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_sources_with_pagination_params(connector):
    route = respx.get(f"{CONTROL_PLANE}/sources").mock(
        return_value=httpx.Response(
            200,
            json={"sources": [{"id": "src_1", "name": "Web JS", "type": "Javascript"}]},
        )
    )
    result = await connector.list_sources(limit=10, after="cursor_xyz")
    assert result["sources"][0]["id"] == "src_1"
    qs = route.calls.last.request.url.params
    assert qs.get("limit") == "10"
    assert qs.get("after") == "cursor_xyz"


@respx.mock
@pytest.mark.asyncio
async def test_get_source(connector):
    respx.get(f"{CONTROL_PLANE}/sources/src_42").mock(
        return_value=httpx.Response(200, json={"id": "src_42", "name": "Main"})
    )
    result = await connector.get_source("src_42")
    assert result["id"] == "src_42"


@respx.mock
@pytest.mark.asyncio
async def test_get_source_not_found(connector):
    respx.get(f"{CONTROL_PLANE}/sources/missing").mock(
        return_value=httpx.Response(404, json={"message": "no such source"})
    )
    with pytest.raises(RudderstackNotFoundError):
        await connector.get_source("missing")


@respx.mock
@pytest.mark.asyncio
async def test_create_source(connector):
    route = respx.post(f"{CONTROL_PLANE}/sources").mock(
        return_value=httpx.Response(
            201,
            json={"id": "src_new", "name": "Mobile", "type": "Javascript"},
        )
    )
    result = await connector.create_source(
        name="Mobile",
        type="Javascript",
        config={"foo": "bar"},
    )
    assert result["id"] == "src_new"
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["name"] == "Mobile"
    assert body["type"] == "Javascript"
    assert body["config"] == {"foo": "bar"}


# ═══════════════════════════════════════════════════════════════════════════
# Destinations
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_destinations(connector):
    respx.get(f"{CONTROL_PLANE}/destinations").mock(
        return_value=httpx.Response(
            200,
            json={"destinations": [{"id": "dst_1", "name": "BQ"}]},
        )
    )
    result = await connector.list_destinations(limit=20)
    assert result["destinations"][0]["id"] == "dst_1"


@respx.mock
@pytest.mark.asyncio
async def test_get_destination(connector):
    respx.get(f"{CONTROL_PLANE}/destinations/dst_42").mock(
        return_value=httpx.Response(200, json={"id": "dst_42", "name": "BQ"})
    )
    result = await connector.get_destination("dst_42")
    assert result["id"] == "dst_42"


# ═══════════════════════════════════════════════════════════════════════════
# Connections
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_connections(connector):
    respx.get(f"{CONTROL_PLANE}/connections").mock(
        return_value=httpx.Response(
            200,
            json={
                "connections": [
                    {"id": "cn_1", "sourceId": "src_1", "destinationId": "dst_1"}
                ]
            },
        )
    )
    result = await connector.list_connections()
    assert result["connections"][0]["id"] == "cn_1"


# ═══════════════════════════════════════════════════════════════════════════
# Profiles / Identities
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_profiles(connector):
    respx.get(f"{CONTROL_PLANE}/profiles").mock(
        return_value=httpx.Response(200, json={"profiles": [{"id": "p_1"}]})
    )
    result = await connector.list_profiles(limit=5)
    assert result["profiles"][0]["id"] == "p_1"


@respx.mock
@pytest.mark.asyncio
async def test_get_profile(connector):
    respx.get(f"{CONTROL_PLANE}/profiles/p_42").mock(
        return_value=httpx.Response(200, json={"id": "p_42"})
    )
    result = await connector.get_profile("p_42")
    assert result["id"] == "p_42"


@respx.mock
@pytest.mark.asyncio
async def test_list_identities(connector):
    respx.get(f"{CONTROL_PLANE}/identities").mock(
        return_value=httpx.Response(200, json={"identities": [{"id": "i_1"}]})
    )
    result = await connector.list_identities()
    assert result["identities"][0]["id"] == "i_1"


# ═══════════════════════════════════════════════════════════════════════════
# Data-plane events
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_track_event_payload(connector):
    route = respx.post(f"{DATA_PLANE}/v1/track").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    out = await connector.track_event(
        user_id="u-1",
        event="Signed Up",
        properties={"plan": "pro"},
    )
    assert out == {"status": "OK"}
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["userId"] == "u-1"
    assert body["event"] == "Signed Up"
    assert body["properties"]["plan"] == "pro"
    assert "timestamp" in body
    assert "sentAt" in body


@respx.mock
@pytest.mark.asyncio
async def test_identify_user_payload(connector):
    route = respx.post(f"{DATA_PLANE}/v1/identify").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    await connector.identify_user(
        user_id="u-2",
        traits={"email": "u@example.com"},
    )
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["userId"] == "u-2"
    assert body["traits"]["email"] == "u@example.com"
    assert "timestamp" in body


@respx.mock
@pytest.mark.asyncio
async def test_page_event_payload(connector):
    route = respx.post(f"{DATA_PLANE}/v1/page").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    await connector.page_event(
        user_id="u-3",
        name="Home",
        properties={"path": "/"},
    )
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["userId"] == "u-3"
    assert body["name"] == "Home"
    assert body["properties"]["path"] == "/"


@respx.mock
@pytest.mark.asyncio
async def test_screen_event_payload(connector):
    route = respx.post(f"{DATA_PLANE}/v1/screen").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    await connector.screen_event(
        user_id="u-3a",
        name="Dashboard",
        properties={"view": "main"},
    )
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["userId"] == "u-3a"
    assert body["name"] == "Dashboard"
    assert body["properties"]["view"] == "main"


@respx.mock
@pytest.mark.asyncio
async def test_group_event_payload(connector):
    route = respx.post(f"{DATA_PLANE}/v1/group").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    await connector.group_event(
        user_id="u-4",
        group_id="org-1",
        traits={"plan": "ent"},
    )
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["userId"] == "u-4"
    assert body["groupId"] == "org-1"
    assert body["traits"]["plan"] == "ent"


@respx.mock
@pytest.mark.asyncio
async def test_alias_user_payload(connector):
    route = respx.post(f"{DATA_PLANE}/v1/alias").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    await connector.alias_user(
        user_id="u-new",
        previous_id="u-old",
    )
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["userId"] == "u-new"
    assert body["previousId"] == "u-old"


@respx.mock
@pytest.mark.asyncio
async def test_batch_events_payload(connector):
    route = respx.post(f"{DATA_PLANE}/v1/batch").mock(
        return_value=httpx.Response(200, json={"status": "OK"})
    )
    events = [
        {"type": "track", "userId": "u-1", "event": "A"},
        {"type": "identify", "userId": "u-1", "traits": {"x": 1}},
    ]
    await connector.batch_events(events=events)
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["batch"] == events
    assert "sentAt" in body


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{CONTROL_PLANE}/sources").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow down"}),
            httpx.Response(200, json={"sources": [{"id": "after-retry"}]}),
        ]
    )
    result = await connector.list_sources(limit=1)
    assert route.call_count == 2
    assert result["sources"][0]["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{CONTROL_PLANE}/sources").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"sources": []}),
        ]
    )
    result = await connector.list_sources()
    assert route.call_count == 2
    assert result == {"sources": []}


# ═══════════════════════════════════════════════════════════════════════════
# Auth-error / not-found paths
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_track_auth_error(connector):
    respx.post(f"{DATA_PLANE}/v1/track").mock(
        return_value=httpx.Response(401, json={"message": "invalid write_key"})
    )
    with pytest.raises(RudderstackAuthError):
        await connector.track_event(user_id="u-1", event="X")


@pytest.mark.asyncio
async def test_control_plane_requires_pat():
    """Calling control-plane methods without a PAT raises RudderstackAuthError."""
    cfg = dict(TEST_CONFIG)
    cfg.pop("access_token")
    c = RudderstackConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )
    with pytest.raises(RudderstackAuthError):
        await c.list_sources()


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert RudderstackConnector.CONNECTOR_TYPE == "rudderstack"


def test_auth_type_class_attr():
    assert RudderstackConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(RudderstackConnector, "REQUIRED_CONFIG_KEYS")
    assert "write_key" in RudderstackConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(RudderstackConnector, "_STATUS_MAP")
    assert 401 in RudderstackConnector._STATUS_MAP
    assert 403 in RudderstackConnector._STATUS_MAP
    assert 429 in RudderstackConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = RudderstackConnector(
        tenant_id="t-A",
        connector_id="conn-1",
        config=dict(TEST_CONFIG),
    )
    c2 = RudderstackConnector(
        tenant_id="t-B",
        connector_id="conn-2",
        config=dict(TEST_CONFIG),
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — id format
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_source_id_format():
    from helpers.normalizer import normalize_source

    raw = {"id": "src_99", "name": "Web", "type": "Javascript"}
    doc = normalize_source(raw, "conn-1", TENANT_ID)
    assert doc.id == f"{TENANT_ID}_src_99"
    assert doc.source_id == "src_99"
    assert doc.metadata["type"] == "Javascript"
    assert doc.metadata["kind"] == "rudderstack.source"


def test_normalize_destination_id_format():
    from helpers.normalizer import normalize_destination

    raw = {"id": "dst_99", "name": "BQ", "type": "BIGQUERY"}
    doc = normalize_destination(raw, "conn-1", TENANT_ID)
    assert doc.id == f"{TENANT_ID}_dst_99"
    assert doc.metadata["kind"] == "rudderstack.destination"
