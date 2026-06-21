"""Unit tests for TogglConnector — respx-mocked, zero real I/O."""
import base64

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import TogglConnector
from exceptions import TogglAuthError, TogglError, TogglNotFound

from tests.conftest import (
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_TOKEN,
    TEST_CONFIG,
    TEST_WORKSPACE_ID,
    TOGGL_BASE,
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
async def test_install_missing_api_token(connector):
    connector.config.pop("api_token", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Basic api_token:api_token) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_basic_with_api_token_literal(connector):
    """Connector must send HTTP Basic with api_token:api_token (literal)."""
    route = respx.get(f"{TOGGL_BASE}/me").mock(
        return_value=httpx.Response(200, json={"id": 1, "email": "a@b.c"})
    )
    await connector.get_me()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth is not None
    assert sent_auth.startswith("Basic ")
    decoded = base64.b64decode(sent_auth.split(" ", 1)[1]).decode()
    assert decoded == f"{TEST_API_TOKEN}:api_token"
    # No Bearer
    assert not sent_auth.lower().startswith("bearer ")


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_toggl_auth_error(connector):
    respx.get(f"{TOGGL_BASE}/me").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    with pytest.raises(TogglAuthError):
        await connector.get_me()


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{TOGGL_BASE}/me").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{TOGGL_BASE}/me").mock(
        return_value=httpx.Response(401, json={"message": "Bad token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Me
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_me_success(connector):
    payload = {"id": 42, "email": "vivek@example.com", "default_workspace_id": 12345}
    respx.get(f"{TOGGL_BASE}/me").mock(return_value=httpx.Response(200, json=payload))
    result = await connector.get_me()
    assert result["id"] == 42
    assert result["default_workspace_id"] == 12345


# ═══════════════════════════════════════════════════════════════════════════
# Workspaces
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_workspaces_success(connector):
    respx.get(f"{TOGGL_BASE}/workspaces").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "Personal"}])
    )
    result = await connector.list_workspaces()
    assert isinstance(result, list)
    assert result[0]["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_get_workspace_not_found(connector):
    wid = 99999
    respx.get(f"{TOGGL_BASE}/workspaces/{wid}").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    with pytest.raises(TogglNotFound):
        await connector.get_workspace(wid)


# ═══════════════════════════════════════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_projects_query_params(connector):
    route = respx.get(f"{TOGGL_BASE}/workspaces/{TEST_WORKSPACE_ID}/projects").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "name": "Alpha"}])
    )
    result = await connector.list_projects(TEST_WORKSPACE_ID, active=True, page=1, per_page=50)
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("active") == "true"
    assert qs.get("page") == "1"
    assert qs.get("per_page") == "50"
    assert result[0]["id"] == 10


@respx.mock
@pytest.mark.asyncio
async def test_create_project_posts_body(connector):
    route = respx.post(f"{TOGGL_BASE}/workspaces/{TEST_WORKSPACE_ID}/projects").mock(
        return_value=httpx.Response(200, json={"id": 100, "name": "New project"})
    )
    project = {"name": "New project", "active": True, "is_private": True}
    result = await connector.create_project(TEST_WORKSPACE_ID, project)
    assert route.called
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == project
    assert result["id"] == 100


# ═══════════════════════════════════════════════════════════════════════════
# Time entries
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_time_entries_date_range(connector):
    route = respx.get(f"{TOGGL_BASE}/me/time_entries").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "description": "hack"}])
    )
    result = await connector.list_time_entries(
        start_date="2026-06-01",
        end_date="2026-06-21",
    )
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("start_date") == "2026-06-01"
    assert qs.get("end_date") == "2026-06-21"
    assert result[0]["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_get_current_time_entry_running(connector):
    respx.get(f"{TOGGL_BASE}/me/time_entries/current").mock(
        return_value=httpx.Response(200, json={"id": 99, "description": "running"})
    )
    result = await connector.get_current_time_entry()
    assert result["id"] == 99


@respx.mock
@pytest.mark.asyncio
async def test_create_time_entry_posts_body(connector):
    route = respx.post(f"{TOGGL_BASE}/workspaces/{TEST_WORKSPACE_ID}/time_entries").mock(
        return_value=httpx.Response(200, json={"id": 555, "description": "Coding"})
    )
    entry = {
        "description": "Coding",
        "duration": -1,
        "start": "2026-06-21T10:00:00Z",
        "workspace_id": TEST_WORKSPACE_ID,
        "created_with": "shielva-toggl-connector",
    }
    result = await connector.create_time_entry(TEST_WORKSPACE_ID, entry)
    assert route.called
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == entry
    assert result["id"] == 555


@respx.mock
@pytest.mark.asyncio
async def test_update_time_entry_put(connector):
    teid = 777
    route = respx.put(
        f"{TOGGL_BASE}/workspaces/{TEST_WORKSPACE_ID}/time_entries/{teid}"
    ).mock(return_value=httpx.Response(200, json={"id": teid, "description": "updated"}))
    result = await connector.update_time_entry(TEST_WORKSPACE_ID, teid, {"description": "updated"})
    assert route.called
    assert result["description"] == "updated"


@respx.mock
@pytest.mark.asyncio
async def test_stop_time_entry_patch(connector):
    teid = 888
    route = respx.patch(
        f"{TOGGL_BASE}/workspaces/{TEST_WORKSPACE_ID}/time_entries/{teid}/stop"
    ).mock(return_value=httpx.Response(200, json={"id": teid, "stop": "2026-06-21T11:00:00Z"}))
    result = await connector.stop_time_entry(TEST_WORKSPACE_ID, teid)
    assert route.called
    assert result["id"] == teid


@respx.mock
@pytest.mark.asyncio
async def test_delete_time_entry(connector):
    teid = 999
    route = respx.delete(
        f"{TOGGL_BASE}/workspaces/{TEST_WORKSPACE_ID}/time_entries/{teid}"
    ).mock(return_value=httpx.Response(200, json={}))
    await connector.delete_time_entry(TEST_WORKSPACE_ID, teid)
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Tags / Clients / Tasks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_tags_success(connector):
    respx.get(f"{TOGGL_BASE}/workspaces/{TEST_WORKSPACE_ID}/tags").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "urgent"}])
    )
    result = await connector.list_tags(TEST_WORKSPACE_ID)
    assert result[0]["name"] == "urgent"


@respx.mock
@pytest.mark.asyncio
async def test_list_clients_success(connector):
    respx.get(f"{TOGGL_BASE}/workspaces/{TEST_WORKSPACE_ID}/clients").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "Acme"}])
    )
    result = await connector.list_clients(TEST_WORKSPACE_ID)
    assert result[0]["name"] == "Acme"


@respx.mock
@pytest.mark.asyncio
async def test_create_client_posts_body(connector):
    route = respx.post(f"{TOGGL_BASE}/workspaces/{TEST_WORKSPACE_ID}/clients").mock(
        return_value=httpx.Response(200, json={"id": 100, "name": "Globex"})
    )
    body_in = {"name": "Globex"}
    result = await connector.create_client(TEST_WORKSPACE_ID, body_in)
    import json as _json
    sent = _json.loads(route.calls[0].request.content.decode())
    assert sent == body_in
    assert result["id"] == 100


@respx.mock
@pytest.mark.asyncio
async def test_list_tasks_success(connector):
    pid = 42
    respx.get(f"{TOGGL_BASE}/workspaces/{TEST_WORKSPACE_ID}/projects/{pid}/tasks").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "build"}])
    )
    result = await connector.list_tasks(TEST_WORKSPACE_ID, pid)
    assert result[0]["name"] == "build"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{TOGGL_BASE}/me").mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(200, json={"id": 7}),
        ]
    )
    result = await connector.get_me()
    assert route.call_count == 2
    assert result["id"] == 7


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{TOGGL_BASE}/me").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"id": 8}),
        ]
    )
    result = await connector.get_me()
    assert route.call_count == 2
    assert result["id"] == 8


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert TogglConnector.CONNECTOR_TYPE == "toggl"


def test_auth_type_class_attr():
    assert TogglConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(TogglConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_token" in TogglConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = TogglConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = TogglConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — SOC: id format
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_project_id_is_connector_scoped():
    from helpers.normalizer import normalize_project
    raw = {"id": 555, "name": "MVP", "workspace_id": 1, "created_at": "2026-01-01T00:00:00Z"}
    doc = normalize_project(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.id == f"{CONNECTOR_ID}_555"
    assert doc.source_id == "555"
    assert doc.tenant_id == TENANT_ID
    assert doc.source == "toggl.project"


def test_normalize_time_entry_id_is_connector_scoped():
    from helpers.normalizer import normalize_time_entry
    raw = {"id": 999, "description": "work", "start": "2026-01-01T00:00:00Z", "duration": 120}
    doc = normalize_time_entry(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.id == f"{CONNECTOR_ID}_999"
    assert doc.source_id == "999"
    assert doc.metadata["duration"] == 120
