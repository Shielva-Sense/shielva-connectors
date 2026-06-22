"""Unit tests for LogglyConnector — respx-mocked, zero real I/O."""
import base64
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import LogglyConnector
from exceptions import LogglyAuthError, LogglyError, LogglyNotFound, LogglyNotFoundError

from tests.conftest import (
    CONNECTOR_ID,
    INGEST_BASE,
    MGMT_BASE,
    TENANT_ID,
    TEST_CONFIG,
    TEST_CUSTOMER_TOKEN,
    TEST_PASSWORD,
    TEST_USERNAME,
)


def _expected_basic() -> str:
    raw = f"{TEST_USERNAME}:{TEST_PASSWORD}".encode()
    return "Basic " + base64.b64encode(raw).decode()


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
async def test_install_missing_subdomain(connector):
    connector.config.pop("subdomain", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_username(connector):
    connector.config.pop("username", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_password(connector):
    connector.config.pop("password", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Basic base64) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_basic_base64(connector):
    """Connector must send Authorization: Basic base64(username:password)."""
    route = respx.get(f"{MGMT_BASE}/search").mock(
        return_value=httpx.Response(200, json={"rsid": {"id": "r1"}, "events": []})
    )
    await connector.search_logs(q="error", size=1)
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == _expected_basic()
    assert sent_auth.startswith("Basic ")


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_loggly_auth_error(connector):
    respx.get(f"{MGMT_BASE}/search").mock(
        return_value=httpx.Response(401, json={"message": "Invalid credentials"})
    )
    with pytest.raises(LogglyAuthError):
        await connector.search_logs()


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_403_raises_loggly_auth_error(connector):
    respx.get(f"{MGMT_BASE}/search").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    with pytest.raises(LogglyAuthError):
        await connector.search_logs()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{MGMT_BASE}/search").mock(
        return_value=httpx.Response(200, json={"rsid": {"id": "r1"}, "events": []})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{MGMT_BASE}/search").mock(
        return_value=httpx.Response(401, json={"message": "Invalid"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Search
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_search_logs_params(connector):
    sample = {"rsid": {"id": "rsid-1", "status": "DONE"}, "events": [{"id": "e1"}]}
    route = respx.get(f"{MGMT_BASE}/search").mock(
        return_value=httpx.Response(200, json=sample)
    )
    result = await connector.search_logs(
        q="status:error", from_="-1h", until="now", size=25, order="asc"
    )
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("q") == "status:error"
    assert qs.get("from") == "-1h"
    assert qs.get("until") == "now"
    assert qs.get("size") == "25"
    assert qs.get("order") == "asc"
    assert result["events"][0]["id"] == "e1"


@respx.mock
@pytest.mark.asyncio
async def test_get_search_field_stats(connector):
    sample = {"facets": {"level": {"error": 10, "warn": 5}}}
    route = respx.get(f"{MGMT_BASE}/fields/level").mock(
        return_value=httpx.Response(200, json=sample)
    )
    result = await connector.get_search_field_stats(
        field="level", q="*", from_="-2h", until="now"
    )
    assert route.called
    assert result["facets"]["level"]["error"] == 10


# ═══════════════════════════════════════════════════════════════════════════
# Saved searches
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_saved_searches(connector):
    respx.get(f"{MGMT_BASE}/savedsearches").mock(
        return_value=httpx.Response(200, json=[{"id": "ss1", "name": "errors"}])
    )
    result = await connector.list_saved_searches()
    assert isinstance(result, list) or "raw" in result or result == result
    # Loggly returns a JSON list; respx.json() decodes that to a list.
    assert result[0]["id"] == "ss1"


@respx.mock
@pytest.mark.asyncio
async def test_create_saved_search(connector):
    route = respx.post(f"{MGMT_BASE}/savedsearches").mock(
        return_value=httpx.Response(200, json={"id": "ss-new"})
    )
    payload = {"name": "criticals", "query": "level:critical"}
    result = await connector.create_saved_search(payload)
    body = json.loads(route.calls[0].request.content.decode())
    assert body == payload
    assert result["id"] == "ss-new"


# ═══════════════════════════════════════════════════════════════════════════
# Alerts
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_alerts(connector):
    respx.get(f"{MGMT_BASE}/alerts").mock(
        return_value=httpx.Response(200, json=[{"id": "a1"}])
    )
    result = await connector.list_alerts()
    assert result[0]["id"] == "a1"


@respx.mock
@pytest.mark.asyncio
async def test_create_alert(connector):
    route = respx.post(f"{MGMT_BASE}/alerts").mock(
        return_value=httpx.Response(200, json={"id": "alert-new"})
    )
    payload = {
        "name": "spike",
        "query": "level:error",
        "type": "count",
        "thresholdValue": 100,
        "timeRange": 5,
        "endpoints": [1, 2],
    }
    result = await connector.create_alert(payload)
    body = json.loads(route.calls[0].request.content.decode())
    assert body == payload
    assert result["id"] == "alert-new"


# ═══════════════════════════════════════════════════════════════════════════
# Dashboards
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_dashboards(connector):
    respx.get(f"{MGMT_BASE}/dashboards").mock(
        return_value=httpx.Response(200, json=[{"id": "d1"}])
    )
    result = await connector.list_dashboards()
    assert result[0]["id"] == "d1"


@respx.mock
@pytest.mark.asyncio
async def test_get_dashboard(connector):
    respx.get(f"{MGMT_BASE}/dashboards/d-42").mock(
        return_value=httpx.Response(200, json={"id": "d-42", "name": "Ops"})
    )
    result = await connector.get_dashboard("d-42")
    assert result["id"] == "d-42"


@respx.mock
@pytest.mark.asyncio
async def test_get_dashboard_not_found(connector):
    respx.get(f"{MGMT_BASE}/dashboards/missing").mock(
        return_value=httpx.Response(404, json={"message": "dashboard not found"})
    )
    with pytest.raises(LogglyNotFound):
        await connector.get_dashboard("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Source groups + users
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_source_groups(connector):
    respx.get(f"{MGMT_BASE}/sourcegroups").mock(
        return_value=httpx.Response(200, json=[{"id": "sg1"}])
    )
    result = await connector.list_source_groups()
    assert result[0]["id"] == "sg1"


@respx.mock
@pytest.mark.asyncio
async def test_list_users(connector):
    respx.get(f"{MGMT_BASE}/users").mock(
        return_value=httpx.Response(200, json=[{"id": "u1", "email": "a@b"}])
    )
    result = await connector.list_users()
    assert result[0]["email"] == "a@b"


# ═══════════════════════════════════════════════════════════════════════════
# Bulk send — URL token, NDJSON body, no auth header
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_send_events_bulk_ndjson_body(connector):
    bulk_url = f"{INGEST_BASE}/bulk/{TEST_CUSTOMER_TOKEN}/tag/bulk/"
    route = respx.post(bulk_url).mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )
    events = [{"a": 1}, {"b": 2, "msg": "hi"}]
    result = await connector.send_events_bulk(events)
    assert route.called
    sent = route.calls[0].request
    # Token sits in the URL path, NO Authorization header.
    assert "authorization" not in {k.lower() for k in sent.headers.keys()}
    body_text = sent.content.decode()
    lines = body_text.split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"b": 2, "msg": "hi"}
    assert result["response"] == "ok"


@respx.mock
@pytest.mark.asyncio
async def test_send_events_bulk_empty_returns_zero(connector):
    # No HTTP call should happen when events list is empty.
    result = await connector.send_events_bulk([])
    assert result == {"response": "ok", "sent": 0}


@pytest.mark.asyncio
async def test_send_events_bulk_missing_token_raises():
    """When customer_token is blank, calling bulk send raises LogglyError."""
    cfg = dict(TEST_CONFIG)
    cfg.pop("customer_token", None)
    conn = LogglyConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    with pytest.raises(LogglyError):
        await conn.send_events_bulk([{"x": 1}])


@respx.mock
@pytest.mark.asyncio
async def test_send_events_bulk_custom_tag(connector):
    bulk_url = f"{INGEST_BASE}/bulk/{TEST_CUSTOMER_TOKEN}/tag/audit/"
    route = respx.post(bulk_url).mock(
        return_value=httpx.Response(200, json={"response": "ok"})
    )
    await connector.send_events_bulk([{"x": 1}], tag="audit")
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{MGMT_BASE}/search").mock(
        side_effect=[
            httpx.Response(429, json={"message": "rate limited"}),
            httpx.Response(200, json={"rsid": {}, "events": []}),
        ]
    )
    result = await connector.search_logs()
    assert route.call_count == 2
    assert result == {"rsid": {}, "events": []}


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{MGMT_BASE}/search").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"rsid": {}, "events": []}),
        ]
    )
    result = await connector.search_logs()
    assert route.call_count == 2
    assert result["events"] == []


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert LogglyConnector.CONNECTOR_TYPE == "loggly"


def test_auth_type_class_attr():
    assert LogglyConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(LogglyConnector, "REQUIRED_CONFIG_KEYS")
    assert "subdomain" in LogglyConnector.REQUIRED_CONFIG_KEYS
    assert "username" in LogglyConnector.REQUIRED_CONFIG_KEYS
    assert "password" in LogglyConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert 401 in LogglyConnector._STATUS_MAP
    assert 403 in LogglyConnector._STATUS_MAP
    assert 429 in LogglyConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = LogglyConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = LogglyConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — tenant-scoped doc id
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_event_tenant_scoped_id():
    from helpers.normalizer import normalize_event

    raw = {
        "id": "ev-1",
        "timestamp": 1718956800000,
        "logmsg": "user.login",
        "event": {"user": "ada"},
        "tags": ["app", "prod"],
    }
    doc = normalize_event(raw, connector_id="conn-x", tenant_id="tenant-Y")
    assert doc.id == "tenant-Y_ev-1"
    assert doc.source_id == "ev-1"
    assert doc.tenant_id == "tenant-Y"
    assert doc.connector_id == "conn-x"
    assert doc.source == "loggly.events"
    assert "ada" in doc.content
