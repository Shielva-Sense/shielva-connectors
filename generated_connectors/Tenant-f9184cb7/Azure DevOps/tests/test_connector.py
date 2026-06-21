"""Unit tests for AzureDevopsConnector — respx-mocked, zero real I/O."""
import base64
import json

import httpx
import pytest
import respx
from shared.base_connector import AuthStatus, ConnectorHealth

from connector import AzureDevOpsConnector, AzureDevopsConnector
from exceptions import (
    AzureDevOpsAuthError,
    AzureDevOpsNotFoundError,
)

from tests.conftest import (
    CONNECTOR_ID,
    ORG_BASE,
    ORGANIZATION,
    PAT,
    SAMPLE_PR,
    SAMPLE_PROJECT,
    SAMPLE_REPO,
    SAMPLE_WORK_ITEM,
    TENANT_ID,
    TEST_CONFIG,
)


def _expected_basic_header(pat: str = PAT) -> str:
    return "Basic " + base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")


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
async def test_install_missing_credentials():
    c = AzureDevopsConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"organization": "", "pat": ""},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_accepts_legacy_personal_access_token_alias():
    """The legacy install field name `personal_access_token` is accepted."""
    c = AzureDevopsConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"organization": ORGANIZATION, "personal_access_token": PAT},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.AUTHENTICATED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (HTTP Basic, empty username) + 401 mapping
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_auth_header_is_http_basic_with_empty_user(connector):
    """Connector must send Basic base64(':<pat>') — empty username, PAT as password."""
    route = respx.get(f"{ORG_BASE}/_apis/projects").mock(
        return_value=httpx.Response(200, json={"count": 0, "value": []})
    )
    await connector.list_projects(top=1)
    assert route.called
    sent_auth = route.calls.last.request.headers.get("Authorization")
    assert sent_auth == _expected_basic_header()
    assert not sent_auth.lower().startswith("bearer ")


@pytest.mark.asyncio
@respx.mock
async def test_api_version_query_param_is_present(connector):
    """Every request URL must carry ?api-version=<version>."""
    route = respx.get(f"{ORG_BASE}/_apis/projects").mock(
        return_value=httpx.Response(200, json={"count": 0, "value": []})
    )
    await connector.list_projects()
    qs = route.calls.last.request.url.params
    assert qs.get("api-version") == "7.1"


@pytest.mark.asyncio
@respx.mock
async def test_auth_error_401_maps_to_token_expired(connector):
    respx.get(f"{ORG_BASE}/_apis/projects").mock(
        return_value=httpx.Response(401, json={"message": "TF400813: PAT invalid"})
    )
    status = await connector.health_check()
    assert status.auth_status == AuthStatus.TOKEN_EXPIRED
    assert status.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{ORG_BASE}/_apis/projects").mock(
        return_value=httpx.Response(200, json={"count": 0, "value": []})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


# ═══════════════════════════════════════════════════════════════════════════
# Projects + teams + users
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_projects(connector):
    route = respx.get(f"{ORG_BASE}/_apis/projects").mock(
        return_value=httpx.Response(200, json={"count": 1, "value": [SAMPLE_PROJECT]})
    )
    result = await connector.list_projects()
    assert route.called
    assert result["value"][0]["id"] == SAMPLE_PROJECT["id"]
    assert "api-version=7.1" in str(route.calls.last.request.url)


@pytest.mark.asyncio
@respx.mock
async def test_get_project(connector):
    route = respx.get(f"{ORG_BASE}/_apis/projects/Shielva").mock(
        return_value=httpx.Response(200, json=SAMPLE_PROJECT)
    )
    result = await connector.get_project("Shielva")
    assert route.called
    assert result["name"] == "Shielva"


@pytest.mark.asyncio
@respx.mock
async def test_get_project_not_found(connector):
    respx.get(f"{ORG_BASE}/_apis/projects/missing").mock(
        return_value=httpx.Response(404, json={"message": "Project not found"})
    )
    with pytest.raises(AzureDevOpsNotFoundError):
        await connector.get_project("missing")


@pytest.mark.asyncio
@respx.mock
async def test_list_teams(connector):
    route = respx.get(f"{ORG_BASE}/_apis/projects/Shielva/teams").mock(
        return_value=httpx.Response(200, json={"value": [{"id": "t1", "name": "Core"}]})
    )
    result = await connector.list_teams("Shielva", top=10)
    assert route.called
    assert result["value"][0]["name"] == "Core"


@pytest.mark.asyncio
@respx.mock
async def test_list_users_hits_vssps_host(connector):
    route = respx.get(
        f"https://vssps.dev.azure.com/{ORGANIZATION}/_apis/graph/users"
    ).mock(return_value=httpx.Response(200, json={"value": []}))
    await connector.list_users()
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Repos
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_repos(connector):
    route = respx.get(f"{ORG_BASE}/Shielva/_apis/git/repositories").mock(
        return_value=httpx.Response(200, json={"value": [SAMPLE_REPO]})
    )
    result = await connector.list_repos("Shielva")
    assert route.called
    assert result["value"][0]["name"] == SAMPLE_REPO["name"]


@pytest.mark.asyncio
@respx.mock
async def test_get_repo(connector):
    route = respx.get(
        f"{ORG_BASE}/Shielva/_apis/git/repositories/repo-001"
    ).mock(return_value=httpx.Response(200, json=SAMPLE_REPO))
    result = await connector.get_repo("Shielva", "repo-001")
    assert route.called
    assert result["id"] == SAMPLE_REPO["id"]


# ═══════════════════════════════════════════════════════════════════════════
# Pull requests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_pull_requests(connector):
    route = respx.get(
        f"{ORG_BASE}/Shielva/_apis/git/repositories/repo-001/pullrequests"
    ).mock(return_value=httpx.Response(200, json={"value": [SAMPLE_PR]}))
    result = await connector.list_pull_requests("Shielva", "repo-001", status="active")
    assert route.called
    assert result["value"][0]["pullRequestId"] == 42
    qs = str(route.calls.last.request.url)
    assert "searchCriteria.status=active" in qs


@pytest.mark.asyncio
@respx.mock
async def test_create_pull_request(connector):
    route = respx.post(
        f"{ORG_BASE}/Shielva/_apis/git/repositories/repo-001/pullrequests"
    ).mock(return_value=httpx.Response(201, json=SAMPLE_PR))
    result = await connector.create_pull_request(
        project="Shielva",
        repository_id="repo-001",
        title="Add Azure DevOps connector",
        source_ref="refs/heads/feature/ado",
        target_ref="refs/heads/main",
        description="initial",
    )
    assert route.called
    sent_body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent_body["sourceRefName"] == "refs/heads/feature/ado"
    assert sent_body["targetRefName"] == "refs/heads/main"
    assert sent_body["title"] == "Add Azure DevOps connector"
    assert result["pullRequestId"] == 42
    assert route.calls.last.request.headers.get("Content-Type") == "application/json"


# ═══════════════════════════════════════════════════════════════════════════
# Work items — WIQL → batch
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_work_items_wiql_then_batch(connector):
    wiql_route = respx.post(f"{ORG_BASE}/Shielva/_apis/wit/wiql").mock(
        return_value=httpx.Response(
            200,
            json={"workItems": [{"id": 101, "url": "..."}, {"id": 102, "url": "..."}]},
        )
    )
    batch_route = respx.get(f"{ORG_BASE}/_apis/wit/workitems").mock(
        return_value=httpx.Response(
            200,
            json={
                "count": 2,
                "value": [SAMPLE_WORK_ITEM, dict(SAMPLE_WORK_ITEM, id=102)],
            },
        )
    )

    result = await connector.list_work_items(
        "Shielva", "SELECT [System.Id] FROM WorkItems"
    )
    assert wiql_route.called and batch_route.called
    assert len(result["value"]) == 2
    wiql_body = json.loads(wiql_route.calls.last.request.content.decode("utf-8"))
    assert wiql_body == {"query": "SELECT [System.Id] FROM WorkItems"}
    batch_url = str(batch_route.calls.last.request.url)
    assert "ids=101%2C102" in batch_url or "ids=101,102" in batch_url


@pytest.mark.asyncio
@respx.mock
async def test_query_work_items_returns_refs_only(connector):
    refs = {"workItems": [{"id": 7, "url": "..."}]}
    route = respx.post(f"{ORG_BASE}/Shielva/_apis/wit/wiql").mock(
        return_value=httpx.Response(200, json=refs)
    )
    result = await connector.query_work_items("Shielva", "SELECT [System.Id] FROM WorkItems")
    assert route.called
    assert result == refs


@pytest.mark.asyncio
@respx.mock
async def test_get_work_item(connector):
    route = respx.get(f"{ORG_BASE}/_apis/wit/workitems/101").mock(
        return_value=httpx.Response(200, json=SAMPLE_WORK_ITEM)
    )
    result = await connector.get_work_item(101)
    assert route.called
    assert result["id"] == 101


@pytest.mark.asyncio
@respx.mock
async def test_create_work_item_json_patch_body_and_content_type(connector):
    route = respx.post(f"{ORG_BASE}/Shielva/_apis/wit/workitems/$Bug").mock(
        return_value=httpx.Response(200, json=SAMPLE_WORK_ITEM)
    )

    result = await connector.create_work_item(
        project="Shielva",
        work_item_type="Bug",
        fields={"System.Title": "Bug in connector", "System.State": "New"},
    )
    assert route.called
    assert (
        route.calls.last.request.headers.get("Content-Type")
        == "application/json-patch+json"
    )
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert isinstance(body, list)
    paths = {op["path"] for op in body}
    assert "/fields/System.Title" in paths
    assert "/fields/System.State" in paths
    assert all(op["op"] == "add" for op in body)
    assert result["id"] == 101


@pytest.mark.asyncio
@respx.mock
async def test_update_work_item_patch(connector):
    route = respx.patch(f"{ORG_BASE}/_apis/wit/workitems/101").mock(
        return_value=httpx.Response(
            200,
            json=dict(
                SAMPLE_WORK_ITEM,
                fields={**SAMPLE_WORK_ITEM["fields"], "System.State": "Resolved"},
            ),
        )
    )
    result = await connector.update_work_item(
        work_item_id=101, fields={"System.State": "Resolved"}
    )
    assert route.called
    assert route.calls.last.request.method == "PATCH"
    assert (
        route.calls.last.request.headers.get("Content-Type")
        == "application/json-patch+json"
    )
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body == [{"op": "add", "path": "/fields/System.State", "value": "Resolved"}]
    assert result["fields"]["System.State"] == "Resolved"


# ═══════════════════════════════════════════════════════════════════════════
# Builds + pipelines + releases
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_builds(connector):
    route = respx.get(f"{ORG_BASE}/Shielva/_apis/build/builds").mock(
        return_value=httpx.Response(200, json={"value": [{"id": 1, "buildNumber": "20260601.1"}]})
    )
    result = await connector.list_builds("Shielva", status_filter="completed")
    assert route.called
    qs = route.calls.last.request.url.params
    assert qs.get("statusFilter") == "completed"
    assert result["value"][0]["buildNumber"] == "20260601.1"


@pytest.mark.asyncio
@respx.mock
async def test_queue_build_serializes_parameters(connector):
    route = respx.post(f"{ORG_BASE}/Shielva/_apis/build/builds").mock(
        return_value=httpx.Response(200, json={"id": 99, "status": "notStarted"})
    )
    result = await connector.queue_build(
        project="Shielva",
        definition_id=42,
        source_branch="refs/heads/main",
        parameters={"env": "prod"},
    )
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["definition"] == {"id": 42}
    assert body["sourceBranch"] == "refs/heads/main"
    # ADO expects parameters as a JSON-encoded string, not a nested object.
    assert isinstance(body["parameters"], str)
    assert json.loads(body["parameters"]) == {"env": "prod"}
    assert result["id"] == 99


@pytest.mark.asyncio
@respx.mock
async def test_list_pipelines(connector):
    route = respx.get(f"{ORG_BASE}/Shielva/_apis/pipelines").mock(
        return_value=httpx.Response(200, json={"value": [{"id": 1, "name": "ci"}]})
    )
    result = await connector.list_pipelines("Shielva")
    assert route.called
    assert result["value"][0]["name"] == "ci"


@pytest.mark.asyncio
@respx.mock
async def test_list_releases_hits_vsrm_host(connector):
    route = respx.get(
        f"https://vsrm.dev.azure.com/{ORGANIZATION}/Shielva/_apis/release/releases"
    ).mock(return_value=httpx.Response(200, json={"value": []}))
    await connector.list_releases("Shielva")
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 → success
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    responses = [
        httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "throttled"}),
        httpx.Response(200, json={"value": [SAMPLE_PROJECT], "count": 1}),
    ]
    route = respx.get(f"{ORG_BASE}/_apis/projects").mock(side_effect=responses)

    result = await connector.list_projects()
    assert route.call_count == 2
    assert result["value"][0]["id"] == SAMPLE_PROJECT["id"]


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    responses = [
        httpx.Response(500, json={"message": "boom"}),
        httpx.Response(200, json={"value": [], "count": 0}),
    ]
    route = respx.get(f"{ORG_BASE}/_apis/projects").mock(side_effect=responses)
    result = await connector.list_projects()
    assert route.call_count == 2
    assert result == {"value": [], "count": 0}


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert AzureDevopsConnector.CONNECTOR_TYPE == "azure_devops"


def test_auth_type_class_attr():
    assert AzureDevopsConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert AzureDevopsConnector.REQUIRED_CONFIG_KEYS == ["organization", "pat"]


def test_status_map_classification_defined():
    assert AzureDevopsConnector._STATUS_MAP[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert AzureDevopsConnector._STATUS_MAP[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert AzureDevopsConnector._STATUS_MAP[429] == ("DEGRADED", "CONNECTED")


def test_back_compat_pascalcase_alias():
    """The historic PascalCase class name must remain a valid alias."""
    assert AzureDevOpsConnector is AzureDevopsConnector


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = AzureDevopsConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = AzureDevopsConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — NormalizedDocument id format
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_work_item_id_format():
    from helpers.normalizer import normalize_work_item

    doc = normalize_work_item(SAMPLE_WORK_ITEM, "conn-xyz", "tenant-abc")
    assert doc.id == "tenant-abc_101"
    assert doc.source_id == "101"
    assert doc.title == "Bug in connector"
    assert doc.metadata["kind"] == "azure_devops.work_item"


# ═══════════════════════════════════════════════════════════════════════════
# mock_AzureDevopsHTTPClient fixture wires through
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_http_client_fixture_intercepts_calls(mock_AzureDevopsHTTPClient):
    mock_AzureDevopsHTTPClient.instance.list_projects.return_value = {
        "value": [SAMPLE_PROJECT],
        "count": 1,
    }
    c = AzureDevopsConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
    result = await c.list_projects()
    assert mock_AzureDevopsHTTPClient.instance.list_projects.await_count == 1
    assert result["value"][0]["id"] == SAMPLE_PROJECT["id"]
