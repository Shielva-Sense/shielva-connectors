"""Unit tests for PostHogConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import PostHogConnector
from exceptions import (
    PostHogAuthError,
    PostHogError,
    PostHogNotFound,
    PostHogNotFoundError,
)

from tests.conftest import (
    CONNECTOR_ID,
    POSTHOG_BASE,
    TENANT_ID,
    TEST_CONFIG,
    TEST_PERSONAL_KEY,
    TEST_PROJECT_ID,
    TEST_PROJECT_KEY,
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
async def test_install_missing_personal_key(connector):
    connector.config.pop("personal_api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_project_id(connector):
    connector.config.pop("project_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer for management API, none for capture)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer_personal_key(connector):
    """Management API must send `Authorization: Bearer <personal_api_key>`."""
    route = respx.get(f"{POSTHOG_BASE}/api/projects").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await connector.list_projects()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_PERSONAL_KEY}"


@respx.mock
@pytest.mark.asyncio
async def test_capture_endpoint_omits_authorization_header(connector):
    """/capture/ must NOT send an Authorization header — api_key goes in body."""
    route = respx.post(f"{POSTHOG_BASE}/capture/").mock(
        return_value=httpx.Response(200, json={"status": 1})
    )
    await connector.capture_event("u-1", "test_event", {"foo": "bar"})
    assert route.called
    auth = route.calls[0].request.headers.get("authorization")
    assert auth is None or auth == ""
    body = json.loads(route.calls[0].request.content.decode())
    assert body["api_key"] == TEST_PROJECT_KEY
    assert body["event"] == "test_event"
    assert body["distinct_id"] == "u-1"
    assert body["properties"] == {"foo": "bar"}


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_posthog_auth_error(connector):
    respx.get(f"{POSTHOG_BASE}/api/projects").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid key"})
    )
    with pytest.raises(PostHogAuthError):
        await connector.list_projects()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}").mock(
        return_value=httpx.Response(200, json={"id": int(TEST_PROJECT_ID), "name": "Prod"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error_401(connector):
    respx.get(f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid key"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_forbidden_403(connector):
    respx.get(f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_projects_success(connector):
    body = {"results": [{"id": 1, "name": "Prod"}, {"id": 2, "name": "Dev"}]}
    respx.get(f"{POSTHOG_BASE}/api/projects").mock(
        return_value=httpx.Response(200, json=body)
    )
    out = await connector.list_projects()
    assert out == body
    assert len(out["results"]) == 2


@respx.mock
@pytest.mark.asyncio
async def test_get_project_default_id(connector):
    respx.get(f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}").mock(
        return_value=httpx.Response(200, json={"id": int(TEST_PROJECT_ID)})
    )
    result = await connector.get_project()
    assert result["id"] == int(TEST_PROJECT_ID)


# ═══════════════════════════════════════════════════════════════════════════
# Capture
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_batch_capture(connector):
    route = respx.post(f"{POSTHOG_BASE}/batch/").mock(
        return_value=httpx.Response(200, json={"status": 1})
    )
    events = [
        {"event": "a", "distinct_id": "u-1", "properties": {}},
        {"event": "b", "distinct_id": "u-2", "properties": {}},
    ]
    out = await connector.batch_capture(events)
    assert out == {"status": 1}
    body = json.loads(route.calls[0].request.content.decode())
    assert body["api_key"] == TEST_PROJECT_KEY
    assert body["batch"] == events


@respx.mock
@pytest.mark.asyncio
async def test_identify_emits_identify_event(connector):
    route = respx.post(f"{POSTHOG_BASE}/capture/").mock(
        return_value=httpx.Response(200, json={"status": 1})
    )
    await connector.identify_person("user-7", {"email": "a@b.com", "plan": "free"})
    body = json.loads(route.calls[0].request.content.decode())
    assert body["event"] == "$identify"
    assert body["distinct_id"] == "user-7"
    assert body["properties"]["$set"]["email"] == "a@b.com"


@respx.mock
@pytest.mark.asyncio
async def test_alias_emits_create_alias(connector):
    route = respx.post(f"{POSTHOG_BASE}/capture/").mock(
        return_value=httpx.Response(200, json={"status": 1})
    )
    await connector.alias_distinct_ids("user-anon-1", "user-known-1")
    body = json.loads(route.calls[0].request.content.decode())
    assert body["event"] == "$create_alias"
    assert body["distinct_id"] == "user-anon-1"
    assert body["properties"]["alias"] == "user-known-1"


@pytest.mark.asyncio
async def test_capture_event_without_project_key_raises():
    c = PostHogConnector(
        TENANT_ID,
        CONNECTOR_ID,
        {"personal_api_key": "phx_x", "project_id": "1"},
    )
    with pytest.raises(PostHogError):
        await c.capture_event("u", "ev")


# ═══════════════════════════════════════════════════════════════════════════
# Feature flags
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_feature_flags(connector):
    body = {"results": [{"id": 1, "key": "new_ui", "active": True}]}
    route = respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/feature_flags"
    ).mock(return_value=httpx.Response(200, json=body))
    out = await connector.list_feature_flags()
    assert out == body
    auth = route.calls[0].request.headers["authorization"]
    assert auth == f"Bearer {TEST_PERSONAL_KEY}"


@respx.mock
@pytest.mark.asyncio
async def test_create_feature_flag(connector):
    body = {"id": 99, "key": "beta_dashboard", "active": True}
    route = respx.post(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/feature_flags"
    ).mock(return_value=httpx.Response(201, json=body))
    out = await connector.create_feature_flag(
        key="beta_dashboard",
        name="Beta Dashboard",
        active=True,
    )
    assert out["id"] == 99
    sent = json.loads(route.calls[0].request.content.decode())
    assert sent["key"] == "beta_dashboard"
    assert sent["name"] == "Beta Dashboard"
    assert sent["active"] is True
    assert "filters" in sent


@respx.mock
@pytest.mark.asyncio
async def test_get_feature_flag_not_found(connector):
    respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/feature_flags/999"
    ).mock(return_value=httpx.Response(404, json={"detail": "flag missing"}))
    with pytest.raises(PostHogNotFound):
        await connector.get_feature_flag(999)


# ═══════════════════════════════════════════════════════════════════════════
# Persons / cohorts
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_persons_with_search(connector):
    body = {"results": [{"id": 1, "distinct_ids": ["u-1"], "properties": {}}]}
    route = respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/persons"
    ).mock(return_value=httpx.Response(200, json=body))
    out = await connector.list_persons(search="alice", limit=50)
    assert out == body
    qs = dict(route.calls[0].request.url.params)
    assert qs["search"] == "alice"
    assert qs["limit"] == "50"


@respx.mock
@pytest.mark.asyncio
async def test_get_person(connector):
    respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/persons/p-1"
    ).mock(return_value=httpx.Response(200, json={"id": "p-1"}))
    out = await connector.get_person("p-1")
    assert out["id"] == "p-1"


@respx.mock
@pytest.mark.asyncio
async def test_list_cohorts(connector):
    body = {"results": [{"id": 7, "name": "Power Users"}]}
    respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/cohorts"
    ).mock(return_value=httpx.Response(200, json=body))
    out = await connector.list_cohorts()
    assert out == body


# ═══════════════════════════════════════════════════════════════════════════
# Insights / dashboards / actions / annotations / experiments
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_insights(connector):
    respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/insights"
    ).mock(return_value=httpx.Response(200, json={"results": []}))
    out = await connector.list_insights()
    assert out == {"results": []}


@respx.mock
@pytest.mark.asyncio
async def test_list_dashboards(connector):
    respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/dashboards"
    ).mock(return_value=httpx.Response(200, json={"results": [{"id": 1}]}))
    out = await connector.list_dashboards()
    assert out["results"][0]["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_get_dashboard(connector):
    respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/dashboards/42"
    ).mock(return_value=httpx.Response(200, json={"id": 42, "name": "Funnel"}))
    out = await connector.get_dashboard(42)
    assert out["id"] == 42


@respx.mock
@pytest.mark.asyncio
async def test_list_actions(connector):
    respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/actions"
    ).mock(return_value=httpx.Response(200, json={"results": []}))
    out = await connector.list_actions()
    assert "results" in out


@respx.mock
@pytest.mark.asyncio
async def test_list_annotations(connector):
    respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/annotations"
    ).mock(return_value=httpx.Response(200, json={"results": []}))
    out = await connector.list_annotations()
    assert "results" in out


@respx.mock
@pytest.mark.asyncio
async def test_list_experiments(connector):
    respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/experiments"
    ).mock(return_value=httpx.Response(200, json={"results": []}))
    out = await connector.list_experiments()
    assert "results" in out


# ═══════════════════════════════════════════════════════════════════════════
# Events + HogQL
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_events(connector):
    body = {"results": [{"id": "e1", "event": "click"}]}
    route = respx.get(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/events"
    ).mock(return_value=httpx.Response(200, json=body))
    out = await connector.list_events(after="2026-01-01", limit=50)
    assert out == body
    qs = dict(route.calls[0].request.url.params)
    assert qs["after"] == "2026-01-01"
    assert qs["limit"] == "50"


@respx.mock
@pytest.mark.asyncio
async def test_run_query(connector):
    body = {"results": [[1, 2], [3, 4]], "types": ["int", "int"]}
    route = respx.post(
        f"{POSTHOG_BASE}/api/projects/{TEST_PROJECT_ID}/query"
    ).mock(return_value=httpx.Response(200, json=body))
    query = {"kind": "HogQLQuery", "query": "select count() from events"}
    out = await connector.run_query(query)
    assert out == body
    sent = json.loads(route.calls[0].request.content.decode())
    assert sent == {"query": query}


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 500
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{POSTHOG_BASE}/api/projects").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "slow"}),
            httpx.Response(200, json={"results": [{"id": 1}]}),
        ]
    )
    out = await connector.list_projects()
    assert route.call_count == 2
    assert out["results"][0]["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{POSTHOG_BASE}/api/projects").mock(
        side_effect=[
            httpx.Response(500, json={"detail": "boom"}),
            httpx.Response(200, json={"results": []}),
        ]
    )
    out = await connector.list_projects()
    assert route.call_count == 2
    assert out == {"results": []}


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert PostHogConnector.CONNECTOR_TYPE == "posthog"


def test_auth_type_class_attr():
    assert PostHogConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(PostHogConnector, "REQUIRED_CONFIG_KEYS")
    assert "personal_api_key" in PostHogConnector.REQUIRED_CONFIG_KEYS
    assert "project_id" in PostHogConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = PostHogConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = PostHogConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
