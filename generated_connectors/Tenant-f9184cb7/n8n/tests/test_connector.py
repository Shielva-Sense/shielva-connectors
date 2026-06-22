"""Unit tests for ``N8nConnector`` — respx-mocked, zero real network I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import N8nConnector
from exceptions import (
    N8nAuthError,
    N8nBadRequestError,
    N8nConflictError,
    N8nNotFound,
    N8nRateLimitError,
)
from helpers.normalizer import normalize_execution, normalize_workflow
from helpers.utils import (
    build_execution_list_params,
    build_paging_params,
    build_workflow_list_params,
)
from tests.conftest import (
    API_BASE,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_instance_url(connector):
    connector.config.pop("instance_url", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_x_n8n_api_key_header_sent(connector):
    """Connector must send the api_key in the ``X-N8N-API-KEY`` header."""
    route = respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(200, json={"data": [], "nextCursor": None}),
    )
    await connector.list_workflows(limit=1)
    assert route.called
    assert route.calls[0].request.headers.get("x-n8n-api-key") == TEST_API_KEY
    # The connector must NOT use Authorization for n8n.
    assert "authorization" not in {
        k.lower() for k in route.calls[0].request.headers.keys()
    }


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_n8n_auth_error(connector):
    respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(401, json={"message": "Invalid API key"}),
    )
    with pytest.raises(N8nAuthError):
        await connector.list_workflows()


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_403_raises_n8n_auth_error(connector):
    respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"}),
    )
    with pytest.raises(N8nAuthError):
        await connector.list_workflows()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(200, json={"data": [], "nextCursor": None}),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error_401_maps_to_token_expired(connector):
    respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"}),
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error_403_maps_to_invalid_credentials(connector):
    respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"}),
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Workflows
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_workflows_no_filter(connector):
    route = respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "1", "name": "WF-1", "active": True},
                    {"id": "2", "name": "WF-2", "active": False},
                ],
                "nextCursor": None,
            },
        ),
    )
    result = await connector.list_workflows()
    assert route.called
    assert len(result["data"]) == 2
    assert result["data"][0]["id"] == "1"


@respx.mock
@pytest.mark.asyncio
async def test_list_workflows_active_filter(connector):
    route = respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "1"}]}),
    )
    await connector.list_workflows(active=True, limit=50)
    sent_url = str(route.calls.last.request.url)
    assert "active=true" in sent_url
    assert "limit=50" in sent_url


@respx.mock
@pytest.mark.asyncio
async def test_list_workflows_with_cursor_and_tags(connector):
    route = respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(200, json={"data": [], "nextCursor": None}),
    )
    await connector.list_workflows(cursor="abc123", tags="prod,public")
    sent = route.calls.last.request
    assert "cursor=abc123" in str(sent.url)
    assert "tags=prod" in str(sent.url)


@respx.mock
@pytest.mark.asyncio
async def test_get_workflow_success(connector):
    respx.get(f"{API_BASE}/workflows/wf-42").mock(
        return_value=httpx.Response(
            200,
            json={"id": "wf-42", "name": "My Workflow", "active": True},
        ),
    )
    result = await connector.get_workflow("wf-42")
    assert result["id"] == "wf-42"


@respx.mock
@pytest.mark.asyncio
async def test_get_workflow_not_found(connector):
    respx.get(f"{API_BASE}/workflows/missing").mock(
        return_value=httpx.Response(404, json={"message": "Workflow not found"}),
    )
    with pytest.raises(N8nNotFound):
        await connector.get_workflow("missing")


@respx.mock
@pytest.mark.asyncio
async def test_create_workflow(connector):
    route = respx.post(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(201, json={"id": "new-1", "name": "Created"}),
    )
    result = await connector.create_workflow(
        name="Created",
        nodes=[{"id": "node-1", "type": "n8n-nodes-base.start"}],
        connections={},
        settings={"saveExecutionProgress": True},
    )
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["name"] == "Created"
    assert body["settings"]["saveExecutionProgress"] is True
    assert result["id"] == "new-1"


@respx.mock
@pytest.mark.asyncio
async def test_update_workflow(connector):
    route = respx.put(f"{API_BASE}/workflows/wf-1").mock(
        return_value=httpx.Response(
            200, json={"id": "wf-1", "name": "Renamed", "active": False},
        ),
    )
    result = await connector.update_workflow("wf-1", name="Renamed", active=False)
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {"name": "Renamed", "active": False}
    assert result["name"] == "Renamed"


@respx.mock
@pytest.mark.asyncio
async def test_activate_workflow(connector):
    route = respx.post(f"{API_BASE}/workflows/wf-1/activate").mock(
        return_value=httpx.Response(200, json={"id": "wf-1", "active": True}),
    )
    result = await connector.activate_workflow("wf-1")
    assert route.called
    assert result["active"] is True


@respx.mock
@pytest.mark.asyncio
async def test_deactivate_workflow(connector):
    route = respx.post(f"{API_BASE}/workflows/wf-1/deactivate").mock(
        return_value=httpx.Response(200, json={"id": "wf-1", "active": False}),
    )
    result = await connector.deactivate_workflow("wf-1")
    assert route.called
    assert result["active"] is False


@respx.mock
@pytest.mark.asyncio
async def test_delete_workflow(connector):
    route = respx.delete(f"{API_BASE}/workflows/wf-1").mock(
        return_value=httpx.Response(200, json={"id": "wf-1"}),
    )
    result = await connector.delete_workflow("wf-1")
    assert route.called
    assert result["id"] == "wf-1"


@respx.mock
@pytest.mark.asyncio
async def test_transfer_workflow(connector):
    route = respx.put(f"{API_BASE}/workflows/wf-1/transfer").mock(
        return_value=httpx.Response(200, json={"id": "wf-1"}),
    )
    result = await connector.transfer_workflow("wf-1", destination_project_id="proj-2")
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {"destinationProjectId": "proj-2"}
    assert result["id"] == "wf-1"


# ═══════════════════════════════════════════════════════════════════════════
# Executions
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_executions_status_filter(connector):
    route = respx.get(f"{API_BASE}/executions").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "e1", "status": "success"}], "nextCursor": None},
        ),
    )
    result = await connector.list_executions(status="success", workflow_id="wf-1")
    sent = str(route.calls.last.request.url)
    assert "status=success" in sent
    assert "workflowId=wf-1" in sent
    assert result["data"][0]["status"] == "success"


@respx.mock
@pytest.mark.asyncio
async def test_get_execution_with_data(connector):
    route = respx.get(f"{API_BASE}/executions/e-99").mock(
        return_value=httpx.Response(200, json={"id": "e-99", "data": {}}),
    )
    await connector.get_execution("e-99", include_data=True)
    assert "includeData=true" in str(route.calls.last.request.url)


@respx.mock
@pytest.mark.asyncio
async def test_delete_execution(connector):
    route = respx.delete(f"{API_BASE}/executions/e-1").mock(
        return_value=httpx.Response(200, json={"id": "e-1"}),
    )
    result = await connector.delete_execution("e-1")
    assert route.called
    assert result["id"] == "e-1"


# ═══════════════════════════════════════════════════════════════════════════
# Credentials
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_credentials(connector):
    respx.get(f"{API_BASE}/credentials").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "c1", "name": "MyCred", "type": "httpHeaderAuth"}]},
        ),
    )
    result = await connector.list_credentials()
    assert result["data"][0]["type"] == "httpHeaderAuth"


@respx.mock
@pytest.mark.asyncio
async def test_get_credential(connector):
    respx.get(f"{API_BASE}/credentials/c-77").mock(
        return_value=httpx.Response(200, json={"id": "c-77", "name": "Slack"}),
    )
    result = await connector.get_credential("c-77")
    assert result["id"] == "c-77"


@respx.mock
@pytest.mark.asyncio
async def test_create_credential(connector):
    route = respx.post(f"{API_BASE}/credentials").mock(
        return_value=httpx.Response(
            201,
            json={"id": "c-new", "name": "Slack Cred", "type": "slackApi"},
        ),
    )
    result = await connector.create_credential(
        name="Slack Cred",
        type="slackApi",
        data={"accessToken": "xoxb-..."},
    )
    body = json.loads(route.calls.last.request.content)
    assert body["name"] == "Slack Cred"
    assert body["type"] == "slackApi"
    assert body["data"] == {"accessToken": "xoxb-..."}
    assert result["id"] == "c-new"


@respx.mock
@pytest.mark.asyncio
async def test_delete_credential(connector):
    route = respx.delete(f"{API_BASE}/credentials/c-1").mock(
        return_value=httpx.Response(200, json={"id": "c-1"}),
    )
    result = await connector.delete_credential("c-1")
    assert route.called
    assert result["id"] == "c-1"


# ═══════════════════════════════════════════════════════════════════════════
# Tags / Users / Variables
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_tags(connector):
    respx.get(f"{API_BASE}/tags").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "t1", "name": "prod"}]}),
    )
    result = await connector.list_tags()
    assert result["data"][0]["name"] == "prod"


@respx.mock
@pytest.mark.asyncio
async def test_create_tag(connector):
    route = respx.post(f"{API_BASE}/tags").mock(
        return_value=httpx.Response(201, json={"id": "t-new", "name": "staging"}),
    )
    result = await connector.create_tag("staging")
    body = json.loads(route.calls.last.request.content)
    assert body == {"name": "staging"}
    assert result["name"] == "staging"


@respx.mock
@pytest.mark.asyncio
async def test_list_users(connector):
    respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "u1", "email": "a@b.co"}]}),
    )
    result = await connector.list_users(limit=5)
    assert result["data"][0]["id"] == "u1"


@respx.mock
@pytest.mark.asyncio
async def test_list_variables(connector):
    respx.get(f"{API_BASE}/variables").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "v1", "key": "FOO"}]}),
    )
    result = await connector.list_variables()
    assert result["data"][0]["key"] == "FOO"


# ═══════════════════════════════════════════════════════════════════════════
# Retry behaviour
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, fast_retries):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{API_BASE}/workflows").mock(
        side_effect=[
            httpx.Response(429, json={"message": "Too Many Requests"}),
            httpx.Response(200, json={"data": [{"id": "1"}], "nextCursor": None}),
        ],
    )
    result = await connector.list_workflows()
    assert route.call_count == 2
    assert result["data"][0]["id"] == "1"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_503_then_success(connector, fast_retries):
    route = respx.get(f"{API_BASE}/workflows/wf-1").mock(
        side_effect=[
            httpx.Response(503, json={"message": "Service Unavailable"}),
            httpx.Response(200, json={"id": "wf-1", "name": "OK"}),
        ],
    )
    result = await connector.get_workflow("wf-1")
    assert route.call_count == 2
    assert result["name"] == "OK"


@respx.mock
@pytest.mark.asyncio
async def test_retry_429_exhausted_raises_rate_limit(connector, fast_retries):
    """All retries return 429 → typed N8nRateLimitError is raised."""
    respx.get(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(429, json={"message": "Too Many"}),
    )
    with pytest.raises(N8nRateLimitError):
        await connector.list_workflows()


@respx.mock
@pytest.mark.asyncio
async def test_retry_after_header_honoured(connector, fast_retries, mocker):
    """When Retry-After is set, _compute_delay must return its value."""
    # Unpatch _compute_delay just for this test
    mocker.stopall()
    mocker.patch("client.http_client.asyncio.sleep")
    route = respx.get(f"{API_BASE}/workflows").mock(
        side_effect=[
            httpx.Response(
                429, headers={"Retry-After": "2"}, json={"message": "slow down"},
            ),
            httpx.Response(200, json={"data": [], "nextCursor": None}),
        ],
    )
    result = await connector.list_workflows()
    assert route.call_count == 2
    assert result == {"data": [], "nextCursor": None}


# ═══════════════════════════════════════════════════════════════════════════
# 400 / 409 typed exceptions
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_400_raises_bad_request(connector):
    respx.post(f"{API_BASE}/workflows").mock(
        return_value=httpx.Response(400, json={"message": "bad body"}),
    )
    with pytest.raises(N8nBadRequestError):
        await connector.create_workflow(name="", nodes=[], connections={})


@respx.mock
@pytest.mark.asyncio
async def test_409_raises_conflict(connector):
    respx.post(f"{API_BASE}/tags").mock(
        return_value=httpx.Response(409, json={"message": "duplicate tag"}),
    )
    with pytest.raises(N8nConflictError):
        await connector.create_tag("dup")


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert N8nConnector.CONNECTOR_TYPE == "n8n"


def test_auth_type_class_attr():
    assert N8nConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert "instance_url" in N8nConnector.REQUIRED_CONFIG_KEYS
    assert "api_key" in N8nConnector.REQUIRED_CONFIG_KEYS


def test_status_map_has_401_403_429():
    assert 401 in N8nConnector._STATUS_MAP
    assert 403 in N8nConnector._STATUS_MAP
    assert 429 in N8nConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_different_tenants_independent_instances():
    c1 = N8nConnector(tenant_id="tenant-A", connector_id="c-1", config=dict(TEST_CONFIG))
    c2 = N8nConnector(tenant_id="tenant-B", connector_id="c-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# base_url normalization
# ═══════════════════════════════════════════════════════════════════════════

def test_base_url_appends_api_v1_when_missing():
    c = N8nConnector(
        tenant_id="t",
        connector_id="c",
        config={"instance_url": "https://x.app.n8n.cloud", "api_key": "k"},
    )
    assert c.base_url == "https://x.app.n8n.cloud/api/v1"


def test_base_url_idempotent_when_already_api_v1():
    c = N8nConnector(
        tenant_id="t",
        connector_id="c",
        config={"instance_url": "https://x.app.n8n.cloud/api/v1", "api_key": "k"},
    )
    assert c.base_url == "https://x.app.n8n.cloud/api/v1"


def test_base_url_strips_trailing_slash():
    c = N8nConnector(
        tenant_id="t",
        connector_id="c",
        config={"instance_url": "https://x.app.n8n.cloud/", "api_key": "k"},
    )
    assert c.base_url == "https://x.app.n8n.cloud/api/v1"


def test_self_hosted_url_works():
    c = N8nConnector(
        tenant_id="t",
        connector_id="c",
        config={"instance_url": "https://n8n.internal.acme.com", "api_key": "k"},
    )
    assert c.base_url == "https://n8n.internal.acme.com/api/v1"


# ═══════════════════════════════════════════════════════════════════════════
# Param builders
# ═══════════════════════════════════════════════════════════════════════════

def test_build_workflow_list_params_active_camelcase():
    params = build_workflow_list_params(active=True, project_id="proj-1", limit=25)
    assert params["active"] == "true"
    assert params["projectId"] == "proj-1"
    assert params["limit"] == 25


def test_build_workflow_list_params_exclude_pinned():
    params = build_workflow_list_params(exclude_pinned_data=True)
    assert params["excludePinnedData"] == "true"


def test_build_execution_list_params_includes_data_camelcase():
    params = build_execution_list_params(
        workflow_id="wf-1", status="success", include_data=True, cursor="abc",
    )
    assert params["workflowId"] == "wf-1"
    assert params["includeData"] == "true"
    assert params["cursor"] == "abc"


def test_build_paging_params_omits_cursor_when_none():
    assert build_paging_params(limit=50) == {"limit": 50}
    assert build_paging_params(limit=50, cursor="next") == {"limit": 50, "cursor": "next"}


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — tenant-scoped IDs
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_workflow_tenant_scoped_id():
    raw = {
        "id": "wf-99",
        "name": "Ship-It",
        "active": True,
        "tags": [{"name": "prod"}, {"name": "critical"}],
        "nodes": [
            {"type": "n8n-nodes-base.webhookTrigger"},
            {"type": "n8n-nodes-base.set"},
        ],
        "createdAt": "2026-06-01T10:00:00.000Z",
        "updatedAt": "2026-06-02T10:00:00.000Z",
    }
    doc = normalize_workflow(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{TENANT_ID}_wf-99"
    assert doc.source_id == "wf-99"
    assert doc.title == "Ship-It"
    assert "prod" in doc.content
    assert doc.metadata["active"] is True
    assert doc.metadata["node_count"] == 2
    assert doc.metadata["has_trigger"] is True
    assert doc.metadata["kind"] == "n8n.workflow"


def test_normalize_execution_tenant_scoped_id():
    raw = {
        "id": "exec-7",
        "workflowId": "wf-99",
        "status": "success",
        "finished": True,
        "startedAt": "2026-06-01T10:00:00.000Z",
        "stoppedAt": "2026-06-01T10:00:05.000Z",
    }
    doc = normalize_execution(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{TENANT_ID}_exec-7"
    assert doc.source_id == "exec-7"
    assert doc.metadata["workflow_id"] == "wf-99"
    assert doc.metadata["status"] == "success"
    assert doc.metadata["kind"] == "n8n.execution"
