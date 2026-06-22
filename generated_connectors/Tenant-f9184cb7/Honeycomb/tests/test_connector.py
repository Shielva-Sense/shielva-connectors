"""Unit tests for HoneycombConnector — respx-mocked, zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import HoneycombConnector
from exceptions import (
    HoneycombAuthError,
    HoneycombError,
    HoneycombNotFoundError,
    HoneycombRateLimitError,
)

from tests.conftest import (
    CONNECTOR_ID,
    HONEYCOMB_BASE,
    SAMPLE_AUTH,
    SAMPLE_COLUMNS,
    SAMPLE_DATASET,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert HoneycombConnector.CONNECTOR_TYPE == "honeycomb"


def test_auth_type_class_attr():
    assert HoneycombConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(HoneycombConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in HoneycombConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(HoneycombConnector, "_STATUS_MAP")
    assert 401 in HoneycombConnector._STATUS_MAP
    assert 403 in HoneycombConnector._STATUS_MAP
    assert 429 in HoneycombConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_install_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/auth").mock(
        return_value=httpx.Response(200, json=SAMPLE_AUTH)
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_key():
    cfg = {k: v for k, v in TEST_CONFIG.items() if k != "api_key"}
    c = HoneycombConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_error_401(connector):
    respx.get(f"{HONEYCOMB_BASE}/auth").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.EXPIRED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (X-Honeycomb-Team, NOT Authorization Bearer)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_team_header_carries_api_key(connector):
    """`X-Honeycomb-Team` MUST carry the api_key; no Bearer Authorization header."""
    route = respx.get(f"{HONEYCOMB_BASE}/auth").mock(
        return_value=httpx.Response(200, json=SAMPLE_AUTH)
    )
    await connector.health_check()
    sent = route.calls.last.request.headers
    assert sent["X-Honeycomb-Team"] == TEST_API_KEY
    auth = sent.get("Authorization")
    assert auth is None or "Bearer" not in auth


# ═══════════════════════════════════════════════════════════════════════════
# health_check() + auth_info()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{HONEYCOMB_BASE}/auth").mock(
        return_value=httpx.Response(200, json=SAMPLE_AUTH)
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_token_expired(connector):
    respx.get(f"{HONEYCOMB_BASE}/auth").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@pytest.mark.asyncio
@respx.mock
async def test_auth_info_returns_team_and_environment(connector):
    respx.get(f"{HONEYCOMB_BASE}/auth").mock(
        return_value=httpx.Response(200, json=SAMPLE_AUTH)
    )
    info = await connector.auth_info()
    assert info["team"]["slug"] == "acme"
    assert info["environment"]["slug"] == "production"


# ═══════════════════════════════════════════════════════════════════════════
# Datasets
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_datasets_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/datasets").mock(
        return_value=httpx.Response(200, json=[SAMPLE_DATASET])
    )
    result = await connector.list_datasets()
    assert isinstance(result, list)
    assert result[0]["slug"] == "my-service"


@pytest.mark.asyncio
@respx.mock
async def test_get_dataset_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/datasets/my-service").mock(
        return_value=httpx.Response(200, json=SAMPLE_DATASET)
    )
    result = await connector.get_dataset("my-service")
    assert result["name"] == "my-service"


@pytest.mark.asyncio
@respx.mock
async def test_get_dataset_not_found(connector):
    respx.get(f"{HONEYCOMB_BASE}/datasets/missing").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    with pytest.raises(HoneycombNotFoundError):
        await connector.get_dataset("missing")


@pytest.mark.asyncio
@respx.mock
async def test_create_dataset_posts_name(connector):
    route = respx.post(f"{HONEYCOMB_BASE}/datasets").mock(
        return_value=httpx.Response(201, json={**SAMPLE_DATASET, "name": "new-service"})
    )
    result = await connector.create_dataset(
        name="new-service", description="A brand-new dataset", expand_json_depth=2
    )
    assert result["name"] == "new-service"
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["name"] == "new-service"
    assert body["description"] == "A brand-new dataset"
    assert body["expand_json_depth"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# Columns
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_columns_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/datasets/my-service/columns").mock(
        return_value=httpx.Response(200, json=SAMPLE_COLUMNS)
    )
    cols = await connector.list_columns("my-service")
    assert len(cols) == 2
    assert cols[0]["key_name"] == "trace.span_id"


# ═══════════════════════════════════════════════════════════════════════════
# Queries
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_queries_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/queries/my-service").mock(
        return_value=httpx.Response(200, json=[{"id": "qry-1"}, {"id": "qry-2"}])
    )
    result = await connector.list_queries("my-service")
    assert len(result) == 2


@pytest.mark.asyncio
@respx.mock
async def test_create_query_posts_breakdowns_and_calculations(connector):
    route = respx.post(f"{HONEYCOMB_BASE}/queries/my-service").mock(
        return_value=httpx.Response(201, json={"id": "qry-123"})
    )
    breakdowns = ["service.name"]
    calculations = [{"op": "COUNT"}, {"op": "AVG", "column": "duration_ms"}]
    filters = [{"column": "error", "op": "=", "value": True}]
    result = await connector.create_query(
        dataset_slug="my-service",
        breakdowns=breakdowns,
        calculations=calculations,
        filters=filters,
        time_range=3600,
        granularity=60,
    )
    assert result["id"] == "qry-123"
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["breakdowns"] == breakdowns
    assert body["calculations"] == calculations
    assert body["filters"] == filters
    assert body["time_range"] == 3600
    assert body["granularity"] == 60


@pytest.mark.asyncio
@respx.mock
async def test_get_query_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/queries/my-service/qry-123").mock(
        return_value=httpx.Response(200, json={"id": "qry-123", "breakdowns": ["x"]})
    )
    result = await connector.get_query("my-service", "qry-123")
    assert result["id"] == "qry-123"


# ═══════════════════════════════════════════════════════════════════════════
# Query results
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_run_query_posts_query_id(connector):
    route = respx.post(f"{HONEYCOMB_BASE}/query_results/my-service").mock(
        return_value=httpx.Response(202, json={"id": "res-1", "complete": False})
    )
    result = await connector.run_query(
        dataset_slug="my-service", query_id="qry-123", disable_series=True, limit=500
    )
    assert result["id"] == "res-1"
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["query_id"] == "qry-123"
    assert body["disable_series"] is True
    assert body["limit"] == 500


@pytest.mark.asyncio
@respx.mock
async def test_get_query_result_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/query_results/my-service/res-1").mock(
        return_value=httpx.Response(
            200,
            json={"id": "res-1", "complete": True, "data": {"results": [{"COUNT": 100}]}},
        )
    )
    result = await connector.get_query_result("my-service", "res-1")
    assert result["complete"] is True
    assert result["data"]["results"][0]["COUNT"] == 100


# ═══════════════════════════════════════════════════════════════════════════
# Markers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_markers_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/markers/my-service").mock(
        return_value=httpx.Response(200, json=[{"id": "mk-1", "message": "deploy v1"}])
    )
    result = await connector.list_markers("my-service")
    assert result[0]["message"] == "deploy v1"


@pytest.mark.asyncio
@respx.mock
async def test_create_marker_posts_message(connector):
    route = respx.post(f"{HONEYCOMB_BASE}/markers/my-service").mock(
        return_value=httpx.Response(201, json={"id": "mk-1", "message": "deploy v1.2.3"})
    )
    result = await connector.create_marker(
        dataset_slug="my-service",
        message="deploy v1.2.3",
        type="deploy",
        url="https://github.com/acme/svc/releases/tag/v1.2.3",
        start_time=1700000000,
    )
    assert result["id"] == "mk-1"
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["message"] == "deploy v1.2.3"
    assert body["type"] == "deploy"
    assert body["start_time"] == 1700000000


# ═══════════════════════════════════════════════════════════════════════════
# Triggers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_triggers_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/triggers/my-service").mock(
        return_value=httpx.Response(200, json=[{"id": "trg-1", "name": "p99 alert"}])
    )
    result = await connector.list_triggers("my-service")
    assert result[0]["name"] == "p99 alert"


@pytest.mark.asyncio
@respx.mock
async def test_create_trigger_posts_threshold_and_recipients(connector):
    route = respx.post(f"{HONEYCOMB_BASE}/triggers/my-service").mock(
        return_value=httpx.Response(201, json={"id": "trg-2", "name": "errors"})
    )
    threshold = {"op": ">", "value": 100}
    recipients = [{"type": "email", "target": "ops@acme.com"}]
    result = await connector.create_trigger(
        dataset_slug="my-service",
        name="errors",
        query_id="qry-err",
        threshold=threshold,
        frequency=600,
        alert_type="on_change",
        recipients=recipients,
    )
    assert result["id"] == "trg-2"
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["threshold"] == threshold
    assert body["recipients"] == recipients
    assert body["frequency"] == 600


# ═══════════════════════════════════════════════════════════════════════════
# Boards
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_boards_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/boards").mock(
        return_value=httpx.Response(200, json=[{"id": "bd-1", "name": "ops"}])
    )
    result = await connector.list_boards()
    assert result[0]["name"] == "ops"


@pytest.mark.asyncio
@respx.mock
async def test_get_board_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/boards/bd-9").mock(
        return_value=httpx.Response(200, json={"id": "bd-9", "name": "errors"})
    )
    result = await connector.get_board("bd-9")
    assert result["id"] == "bd-9"


@pytest.mark.asyncio
@respx.mock
async def test_create_board_posts_queries(connector):
    route = respx.post(f"{HONEYCOMB_BASE}/boards").mock(
        return_value=httpx.Response(201, json={"id": "bd-9", "name": "errors"})
    )
    queries = [{"caption": "errors", "query_id": "qry-err", "dataset": "my-service"}]
    result = await connector.create_board(
        name="errors", description="Error tracking", style="visual", queries=queries
    )
    assert result["id"] == "bd-9"
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["queries"] == queries
    assert body["style"] == "visual"


# ═══════════════════════════════════════════════════════════════════════════
# SLOs
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_slos_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/slos/my-service").mock(
        return_value=httpx.Response(200, json=[{"id": "slo-1", "name": "availability"}])
    )
    result = await connector.list_slos("my-service")
    assert result[0]["name"] == "availability"


# ═══════════════════════════════════════════════════════════════════════════
# Recipients
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_recipients_success(connector):
    respx.get(f"{HONEYCOMB_BASE}/recipients").mock(
        return_value=httpx.Response(
            200, json=[{"id": "rcp-1", "type": "email", "target": "ops@acme.com"}]
        )
    )
    result = await connector.list_recipients()
    assert result[0]["target"] == "ops@acme.com"


# ═══════════════════════════════════════════════════════════════════════════
# Events (ingest)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_send_event_posts_payload(connector):
    route = respx.post(f"{HONEYCOMB_BASE}/events/my-service").mock(
        return_value=httpx.Response(200, json={"status": 202})
    )
    event = {"trace.span_id": "abc", "duration_ms": 12.3, "service.name": "checkout"}
    await connector.send_event("my-service", event)
    body = _json.loads(route.calls.last.request.read().decode())
    assert body == event
    sent = route.calls.last.request.headers
    assert sent["X-Honeycomb-Team"] == TEST_API_KEY


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """First call returns 429, second returns 200 — client must retry transparently."""
    route = respx.get(f"{HONEYCOMB_BASE}/datasets").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "1"}),
            httpx.Response(200, json=[SAMPLE_DATASET]),
        ]
    )
    result = await connector.list_datasets()
    assert isinstance(result, list)
    assert result[0]["slug"] == "my-service"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{HONEYCOMB_BASE}/datasets").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json=[SAMPLE_DATASET]),
        ]
    )
    result = await connector.list_datasets()
    assert isinstance(result, list)
    assert route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# sync() — dataset metadata sync
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_sync_datasets_full(connector):
    respx.get(f"{HONEYCOMB_BASE}/datasets").mock(
        return_value=httpx.Response(200, json=[SAMPLE_DATASET])
    )
    respx.get(f"{HONEYCOMB_BASE}/datasets/my-service/columns").mock(
        return_value=httpx.Response(200, json=SAMPLE_COLUMNS)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = HoneycombConnector(tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = HoneycombConnector(tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Region resolution
# ═══════════════════════════════════════════════════════════════════════════

def test_region_eu_uses_eu_base_url():
    cfg = {**TEST_CONFIG, "region": "eu", "base_url": ""}
    c = HoneycombConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    assert c.base_url == "https://api.eu1.honeycomb.io/1"


def test_region_us_default():
    cfg = {**TEST_CONFIG, "region": "us", "base_url": ""}
    c = HoneycombConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    assert c.base_url == "https://api.honeycomb.io/1"


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_dataset_tenant_scoped_id():
    from helpers.normalizer import normalize_dataset

    doc = normalize_dataset(
        SAMPLE_DATASET,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        columns=SAMPLE_COLUMNS,
    )
    assert doc.id == f"{TENANT_ID}_my-service"
    assert doc.source_id == "my-service"
    assert "trace.span_id" in doc.content
    assert doc.metadata["kind"] == "honeycomb.dataset"


# ═══════════════════════════════════════════════════════════════════════════
# mock_HoneycombHTTPClient fixture smoke
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_mock_http_client_fixture_isolates_connector(mock_HoneycombHTTPClient):
    """The mock_HoneycombHTTPClient fixture must replace the real HTTP client."""
    mock_HoneycombHTTPClient.get_auth.return_value = {"team": {"slug": "fake"}}
    c = HoneycombConnector(
        tenant_id="t", connector_id="c", config=dict(TEST_CONFIG)
    )
    info = await c.auth_info()
    assert info["team"]["slug"] == "fake"
    mock_HoneycombHTTPClient.get_auth.assert_awaited_once()
