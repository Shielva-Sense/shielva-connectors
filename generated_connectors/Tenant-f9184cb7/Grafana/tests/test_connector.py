"""Unit tests for GrafanaConnector — respx-mocked, zero real I/O."""
import asyncio

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus
from connector import GrafanaConnector
from exceptions import GrafanaAuthError, GrafanaNotFound, GrafanaRateLimitError

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    SAMPLE_DASHBOARD_FULL,
    SAMPLE_DASHBOARD_HIT,
    TENANT_ID,
    TEST_CONFIG,
    TOKEN,
)

# Silence retry backoff so tests don't sleep for seconds.
@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast(_s):  # pragma: no cover
        return None
    monkeypatch.setattr(asyncio, "sleep", _fast)


# ─────────────────────────────────────────────────────────────────────────
# install()
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_instance_url(connector):
    connector.instance_url = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_token(connector):
    connector.service_account_token = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ─────────────────────────────────────────────────────────────────────────
# health_check()
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{BASE_URL}/api/health").mock(
        return_value=httpx.Response(200, json={"database": "ok", "version": "10.4.0"})
    )
    status = await connector.health_check()
    assert status.health == ConnectorHealth.HEALTHY
    assert status.auth_status == AuthStatus.CONNECTED
    assert "database=ok" in status.message


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error(connector):
    respx.get(f"{BASE_URL}/api/health").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    status = await connector.health_check()
    assert status.health == ConnectorHealth.DEGRADED
    assert status.auth_status == AuthStatus.TOKEN_EXPIRED


# ─────────────────────────────────────────────────────────────────────────
# Header / Bearer auth verification
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_bearer_token_is_sent(connector):
    route = respx.get(f"{BASE_URL}/api/org").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "Main"})
    )
    result = await connector.get_org()
    assert result["id"] == 1
    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


# ─────────────────────────────────────────────────────────────────────────
# list_dashboards() (+ filters)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_list_dashboards_with_filter(connector):
    route = respx.get(f"{BASE_URL}/api/search").mock(
        return_value=httpx.Response(200, json=[SAMPLE_DASHBOARD_HIT])
    )
    result = await connector.list_dashboards(query="prod", tag=["core"], folder_uids=["folder-uid-1"])
    assert isinstance(result, list)
    assert result[0]["uid"] == "dash-uid-1"
    request = route.calls[0].request
    qs = request.url.params
    assert qs.get("type") == "dash-db"
    assert qs.get("query") == "prod"
    assert qs.get("tag") == "core"
    assert qs.get("folderUIDs") == "folder-uid-1"


# ─────────────────────────────────────────────────────────────────────────
# get_dashboard()
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_get_dashboard_success(connector):
    respx.get(f"{BASE_URL}/api/dashboards/uid/dash-uid-1").mock(
        return_value=httpx.Response(200, json=SAMPLE_DASHBOARD_FULL)
    )
    result = await connector.get_dashboard("dash-uid-1")
    assert result["dashboard"]["uid"] == "dash-uid-1"


@pytest.mark.asyncio
@respx.mock
async def test_get_dashboard_not_found(connector):
    respx.get(f"{BASE_URL}/api/dashboards/uid/missing").mock(
        return_value=httpx.Response(404, json={"message": "Dashboard not found"})
    )
    with pytest.raises(GrafanaNotFound):
        await connector.get_dashboard("missing")


# ─────────────────────────────────────────────────────────────────────────
# create_dashboard()
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_create_dashboard(connector):
    route = respx.post(f"{BASE_URL}/api/dashboards/db").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 100, "uid": "new-dash", "url": "/d/new-dash/new", "status": "success", "version": 1,
            },
        )
    )
    dash = {"title": "New", "panels": []}
    result = await connector.create_dashboard(
        dashboard=dash, folder_uid="folder-uid-1", overwrite=True,
    )
    assert result["status"] == "success"
    body = route.calls[0].request.read()
    import json
    payload = json.loads(body)
    assert payload["dashboard"]["title"] == "New"
    assert payload["folderUid"] == "folder-uid-1"
    assert payload["overwrite"] is True


# ─────────────────────────────────────────────────────────────────────────
# delete_dashboard()
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_delete_dashboard(connector):
    respx.delete(f"{BASE_URL}/api/dashboards/uid/dash-uid-1").mock(
        return_value=httpx.Response(200, json={"message": "Dashboard deleted"})
    )
    result = await connector.delete_dashboard("dash-uid-1")
    assert result["message"] == "Dashboard deleted"


# ─────────────────────────────────────────────────────────────────────────
# list_folders / create_folder
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_list_folders(connector):
    respx.get(f"{BASE_URL}/api/folders").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "uid": "f-1", "title": "Ops"}, {"id": 2, "uid": "f-2", "title": "Eng"}]
        )
    )
    result = await connector.list_folders()
    assert len(result) == 2
    assert result[0]["uid"] == "f-1"


@pytest.mark.asyncio
@respx.mock
async def test_create_folder(connector):
    respx.post(f"{BASE_URL}/api/folders").mock(
        return_value=httpx.Response(200, json={"id": 3, "uid": "f-3", "title": "New"})
    )
    result = await connector.create_folder(title="New", uid="f-3")
    assert result["uid"] == "f-3"


# ─────────────────────────────────────────────────────────────────────────
# Datasources
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_list_datasources(connector):
    respx.get(f"{BASE_URL}/api/datasources").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "uid": "ds-1", "name": "Prometheus", "type": "prometheus"},
        ])
    )
    result = await connector.list_datasources()
    assert result[0]["type"] == "prometheus"


@pytest.mark.asyncio
@respx.mock
async def test_create_datasource(connector):
    route = respx.post(f"{BASE_URL}/api/datasources").mock(
        return_value=httpx.Response(
            200,
            json={"datasource": {"id": 9, "uid": "ds-9", "name": "Loki", "type": "loki"}, "id": 9},
        )
    )
    result = await connector.create_datasource(
        name="Loki", type="loki", url="http://loki:3100",
    )
    assert result["id"] == 9
    import json
    payload = json.loads(route.calls[0].request.read())
    assert payload["name"] == "Loki"
    assert payload["type"] == "loki"
    assert payload["access"] == "proxy"
    assert payload["isDefault"] is False


@pytest.mark.asyncio
@respx.mock
async def test_query_datasource(connector):
    route = respx.post(f"{BASE_URL}/api/ds/query").mock(
        return_value=httpx.Response(200, json={"results": {"A": {"frames": []}}})
    )
    result = await connector.query_datasource(
        datasource_id=1,
        queries=[{"refId": "A", "expr": "up"}],
        from_time=1700000000,
        to_time=1700000600,
    )
    assert "results" in result
    import json
    payload = json.loads(route.calls[0].request.read())
    assert payload["queries"][0]["datasourceId"] == 1
    assert payload["from"] == "1700000000"
    assert payload["to"] == "1700000600"


# ─────────────────────────────────────────────────────────────────────────
# Alert rules / users / teams
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_list_alert_rules(connector):
    respx.get(f"{BASE_URL}/api/v1/provisioning/alert-rules").mock(
        return_value=httpx.Response(200, json=[{"uid": "rule-1", "title": "High CPU"}])
    )
    result = await connector.list_alert_rules(limit=50)
    assert result[0]["uid"] == "rule-1"


@pytest.mark.asyncio
@respx.mock
async def test_list_users(connector):
    respx.get(f"{BASE_URL}/api/users").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "login": "admin", "email": "a@example.com"}])
    )
    result = await connector.list_users()
    assert result[0]["login"] == "admin"


@pytest.mark.asyncio
@respx.mock
async def test_list_teams(connector):
    respx.get(f"{BASE_URL}/api/teams/search").mock(
        return_value=httpx.Response(200, json={"totalCount": 1, "teams": [{"id": 1, "name": "SRE"}]})
    )
    result = await connector.list_teams(query="SRE")
    assert result["teams"][0]["name"] == "SRE"


# ─────────────────────────────────────────────────────────────────────────
# Retry on 429
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success(connector):
    """HTTP client should retry on 429 and succeed on the second attempt."""
    route = respx.get(f"{BASE_URL}/api/org").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "Too Many Requests"}),
            httpx.Response(200, json={"id": 1, "name": "Main"}),
        ]
    )
    result = await connector.get_org()
    assert result["id"] == 1
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_429_exhausts_retries_raises(connector):
    """If 429 persists past all attempts, GrafanaRateLimitError surfaces."""
    respx.get(f"{BASE_URL}/api/org").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "TMR"})
    )
    # Drop the inner http client retry budget for this test to fail fast.
    connector.http_client._max_retries = 1
    with pytest.raises(GrafanaRateLimitError):
        # with_retry wraps it once more; one more 429 will surface the error.
        await connector.get_org()


# ─────────────────────────────────────────────────────────────────────────
# Sync()
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_sync_full_success(connector):
    respx.get(f"{BASE_URL}/api/search").mock(
        return_value=httpx.Response(200, json=[SAMPLE_DASHBOARD_HIT])
    )
    respx.get(f"{BASE_URL}/api/dashboards/uid/dash-uid-1").mock(
        return_value=httpx.Response(200, json=SAMPLE_DASHBOARD_FULL)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


# ─────────────────────────────────────────────────────────────────────────
# Connector identity
# ─────────────────────────────────────────────────────────────────────────

def test_connector_type_attribute():
    assert GrafanaConnector.CONNECTOR_TYPE == "grafana"


def test_auth_type_attribute():
    assert GrafanaConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert "instance_url" in GrafanaConnector.REQUIRED_CONFIG_KEYS
    assert "service_account_token" in GrafanaConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    """OCP: _STATUS_MAP is a public class const for HTTP-status classification."""
    assert hasattr(GrafanaConnector, "_STATUS_MAP")
    assert 401 in GrafanaConnector._STATUS_MAP
    assert 403 in GrafanaConnector._STATUS_MAP
    assert 429 in GrafanaConnector._STATUS_MAP


def test_normalizer_id_is_tenant_scoped():
    """NormalizedDocument id = f'{tenant_id}_{source_id}' — tenant-scoped, never connector-scoped."""
    from helpers.normalizer import normalize_dashboard

    hit = dict(SAMPLE_DASHBOARD_HIT)
    full = dict(SAMPLE_DASHBOARD_FULL)
    doc = normalize_dashboard(hit, full, "conn-xyz", TENANT_ID, base_url=BASE_URL)
    assert doc.id == f"{TENANT_ID}_{hit['uid']}"
    assert doc.source_id == hit["uid"]
    assert doc.tenant_id == TENANT_ID
    assert doc.source == "grafana"


@pytest.mark.asyncio
async def test_multi_tenant_isolation():
    c1 = GrafanaConnector(tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = GrafanaConnector(tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
