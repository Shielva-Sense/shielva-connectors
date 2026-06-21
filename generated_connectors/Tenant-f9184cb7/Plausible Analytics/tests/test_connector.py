"""Unit tests for PlausibleConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx
from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import PlausibleConnector
from exceptions import (
    PlausibleAPIError,
    PlausibleAuthError,
    PlausibleError,
    PlausibleNotFound,
)

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
    TEST_SITE_ID,
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
async def test_install_missing_api_key(connector):
    connector.config.pop("api_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_authorize_returns_apikey_token(connector):
    token = await connector.authorize()
    assert token.access_token == TEST_API_KEY
    assert token.token_type == "api_key"


# ═══════════════════════════════════════════════════════════════════════════
# Bearer header shape + auth-error mapping
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_bearer_header_on_stats(connector):
    """Stats calls must carry Authorization: Bearer <api_key>."""
    route = respx.get(f"{BASE_URL}/stats/realtime/visitors").mock(
        return_value=httpx.Response(200, json=1)
    )
    await connector.realtime_visitors(TEST_SITE_ID)
    sent_auth = route.calls.last.request.headers["Authorization"]
    assert sent_auth == f"Bearer {TEST_API_KEY}"


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{BASE_URL}/stats/realtime/visitors").mock(
        return_value=httpx.Response(200, json=42)
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error(connector):
    respx.get(f"{BASE_URL}/stats/realtime/visitors").mock(
        return_value=httpx.Response(401, json={"error": "invalid_token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_falls_back_to_sites_without_default_site(connector):
    """When default_site_id is blank, health_check probes /sites instead."""
    connector.default_site_id = ""
    route = respx.get(f"{BASE_URL}/sites").mock(
        return_value=httpx.Response(200, json={"sites": []})
    )
    result = await connector.health_check()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# aggregate()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_aggregate_with_metrics_and_filters(connector):
    route = respx.get(f"{BASE_URL}/stats/aggregate").mock(
        return_value=httpx.Response(
            200,
            json={"results": {"visitors": {"value": 1234}, "pageviews": {"value": 5678}}},
        )
    )
    result = await connector.aggregate(
        site_id="example.com",
        period="7d",
        metrics=["visitors", "pageviews"],
        filters="event:page==/pricing",
    )
    assert result["results"]["visitors"]["value"] == 1234
    assert route.called
    sent = route.calls.last.request
    assert "site_id=example.com" in str(sent.url)
    assert "period=7d" in str(sent.url)
    assert sent.headers["Authorization"] == f"Bearer {TEST_API_KEY}"


@pytest.mark.asyncio
@respx.mock
async def test_aggregate_default_metrics(connector):
    route = respx.get(f"{BASE_URL}/stats/aggregate").mock(
        return_value=httpx.Response(200, json={"results": {}})
    )
    await connector.aggregate(site_id="example.com")
    qs = route.calls.last.request.url.params
    metrics = qs.get("metrics", "")
    assert "visitors" in metrics
    assert "pageviews" in metrics
    assert "bounce_rate" in metrics
    assert "visit_duration" in metrics


# ═══════════════════════════════════════════════════════════════════════════
# timeseries()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_timeseries_with_interval(connector):
    route = respx.get(f"{BASE_URL}/stats/timeseries").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"date": "2026-06-01", "visitors": 100}]},
        )
    )
    result = await connector.timeseries(
        site_id="example.com",
        period="30d",
        interval="month",
        metrics=["visitors"],
    )
    assert result["results"][0]["visitors"] == 100
    sent_url = str(route.calls.last.request.url)
    assert "interval=month" in sent_url


# ═══════════════════════════════════════════════════════════════════════════
# breakdown()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_breakdown_property_and_page(connector):
    route = respx.get(f"{BASE_URL}/stats/breakdown").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"page": "/pricing", "visitors": 50},
                    {"page": "/blog", "visitors": 30},
                ]
            },
        )
    )
    result = await connector.breakdown(
        site_id="example.com",
        property="event:page",
        page=2,
        limit=50,
    )
    sent_url = str(route.calls.last.request.url)
    assert "property=event%3Apage" in sent_url or "property=event:page" in sent_url
    assert "page=2" in sent_url
    assert "limit=50" in sent_url
    # connector adds a normalized projection
    assert "normalized" in result
    assert result["normalized"][0]["dimension"] == {"page": "/pricing"}
    assert result["normalized"][0]["metrics"] == {"visitors": 50}


# ═══════════════════════════════════════════════════════════════════════════
# realtime_visitors()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_realtime_visitors_wraps_integer(connector):
    respx.get(f"{BASE_URL}/stats/realtime/visitors").mock(
        return_value=httpx.Response(200, json=17)
    )
    result = await connector.realtime_visitors("example.com")
    assert result == {"visitors": 17}


# ═══════════════════════════════════════════════════════════════════════════
# Events API — no Bearer auth, body shape
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_record_pageview_body_shape(connector):
    route = respx.post(f"{BASE_URL}/events").mock(
        return_value=httpx.Response(202, content=b"")
    )
    await connector.record_pageview(
        domain="example.com",
        url="https://example.com/pricing",
        user_agent="Mozilla/5.0 (test)",
        referrer="https://google.com",
        screen_width=1920,
    )
    sent = route.calls.last.request
    body = json.loads(sent.content.decode())
    assert body["name"] == "pageview"
    assert body["url"] == "https://example.com/pricing"
    assert body["domain"] == "example.com"
    assert body["referrer"] == "https://google.com"
    assert body["screen_width"] == 1920
    # Events endpoint must NOT carry the Bearer header
    assert "Authorization" not in sent.headers
    assert sent.headers["User-Agent"] == "Mozilla/5.0 (test)"


@pytest.mark.asyncio
@respx.mock
async def test_record_custom_event_with_props(connector):
    route = respx.post(f"{BASE_URL}/events").mock(
        return_value=httpx.Response(202, content=b"")
    )
    await connector.record_custom_event(
        domain="example.com",
        name="Signup",
        url="https://example.com/signup",
        props={"plan": "pro", "trial": True},
    )
    body = json.loads(route.calls.last.request.content.decode())
    assert body["name"] == "Signup"
    assert body["props"] == {"plan": "pro", "trial": True}
    assert body["domain"] == "example.com"


@pytest.mark.asyncio
@respx.mock
async def test_send_event_alias_matches_custom_event(connector):
    route = respx.post(f"{BASE_URL}/events").mock(
        return_value=httpx.Response(202, content=b"")
    )
    await connector.send_event(
        domain="example.com",
        name="Login",
        url="https://example.com/login",
    )
    body = json.loads(route.calls.last.request.content.decode())
    assert body["name"] == "Login"


# ═══════════════════════════════════════════════════════════════════════════
# Sites Provisioning API
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_sites_success(connector):
    respx.get(f"{BASE_URL}/sites").mock(
        return_value=httpx.Response(200, json={"sites": [{"domain": "a.com"}]})
    )
    result = await connector.list_sites()
    assert result["sites"][0]["domain"] == "a.com"


@pytest.mark.asyncio
@respx.mock
async def test_create_site(connector):
    route = respx.post(f"{BASE_URL}/sites").mock(
        return_value=httpx.Response(
            201,
            json={"domain": "new.example.com", "timezone": "America/New_York"},
        )
    )
    result = await connector.create_site(
        domain="new.example.com",
        timezone="America/New_York",
    )
    assert result["domain"] == "new.example.com"
    body = json.loads(route.calls.last.request.content.decode())
    assert body == {"domain": "new.example.com", "timezone": "America/New_York"}
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TEST_API_KEY}"


@pytest.mark.asyncio
@respx.mock
async def test_update_site(connector):
    route = respx.put(f"{BASE_URL}/sites/example.com").mock(
        return_value=httpx.Response(200, json={"domain": "example.com", "timezone": "UTC"})
    )
    result = await connector.update_site("example.com", timezone="UTC")
    assert result["timezone"] == "UTC"
    body = json.loads(route.calls.last.request.content.decode())
    assert body == {"timezone": "UTC"}


@pytest.mark.asyncio
@respx.mock
async def test_delete_site(connector):
    respx.delete(f"{BASE_URL}/sites/example.com").mock(
        return_value=httpx.Response(204, content=b"")
    )
    result = await connector.delete_site("example.com")
    assert result == {"deleted": True}


# ═══════════════════════════════════════════════════════════════════════════
# Goals
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_goals(connector):
    respx.get(f"{BASE_URL}/sites/example.com/goals").mock(
        return_value=httpx.Response(
            200,
            json={"goals": [{"id": "g1", "goal_type": "event", "event_name": "Signup"}]},
        )
    )
    result = await connector.list_goals("example.com")
    assert result["goals"][0]["event_name"] == "Signup"


@pytest.mark.asyncio
async def test_create_goal_event_requires_event_name(connector):
    with pytest.raises(PlausibleAPIError):
        await connector.create_goal(site_id="example.com", goal_type="event")


@pytest.mark.asyncio
async def test_create_goal_page_requires_page_path(connector):
    with pytest.raises(PlausibleAPIError):
        await connector.create_goal(site_id="example.com", goal_type="page")


@pytest.mark.asyncio
@respx.mock
async def test_create_goal_event_happy(connector):
    route = respx.post(f"{BASE_URL}/sites/example.com/goals").mock(
        return_value=httpx.Response(
            201,
            json={"id": "g1", "goal_type": "event", "event_name": "Signup"},
        )
    )
    await connector.create_goal(
        site_id="example.com",
        goal_type="event",
        event_name="Signup",
    )
    body = json.loads(route.calls.last.request.content.decode())
    assert body == {
        "goal_type": "event",
        "event_name": "Signup",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/stats/realtime/visitors").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"}),
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"}),
            httpx.Response(200, json=99),
        ]
    )
    result = await connector.realtime_visitors("example.com")
    assert result == {"visitors": 99}
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/stats/realtime/visitors").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json=7),
        ]
    )
    result = await connector.realtime_visitors("example.com")
    assert result == {"visitors": 7}
    assert route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# Error propagation
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_aggregate_auth_error_propagates(connector):
    respx.get(f"{BASE_URL}/stats/aggregate").mock(
        return_value=httpx.Response(401, json={"error": "invalid_token"})
    )
    with pytest.raises(PlausibleAuthError):
        await connector.aggregate(site_id="example.com")


@pytest.mark.asyncio
@respx.mock
async def test_get_site_not_found(connector):
    respx.get(f"{BASE_URL}/sites/missing.example.com").mock(
        return_value=httpx.Response(404, json={"error": "not_found"})
    )
    with pytest.raises(PlausibleNotFound):
        await connector.get_site("missing.example.com")


# ═══════════════════════════════════════════════════════════════════════════
# Sync — happy path materialises a NormalizedDocument
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_sync_snapshot(connector):
    respx.get(f"{BASE_URL}/stats/aggregate").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": {
                    "visitors": {"value": 500},
                    "pageviews": {"value": 1200},
                    "bounce_rate": {"value": 31.2},
                    "visit_duration": {"value": 95},
                }
            },
        )
    )
    respx.get(f"{BASE_URL}/stats/realtime/visitors").mock(
        return_value=httpx.Response(200, json=12)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_synced == 1
    assert result.documents_found == 1
    # ingest_document was called once
    PlausibleConnector.ingest_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_missing_default_site_fails_softly(connector):
    connector.default_site_id = ""
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "default_site_id" in (result.message or "")


# ═══════════════════════════════════════════════════════════════════════════
# Connector → HTTPClient wiring (via mock_PlausibleHTTPClient fixture)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_breakdown_delegates_to_http_client(connector, mock_PlausibleHTTPClient):
    """Verify the connector forwards args verbatim to PlausibleHTTPClient."""
    mock_PlausibleHTTPClient.get_breakdown.return_value = {
        "results": [{"page": "/x", "visitors": 5}],
    }
    connector.http_client = mock_PlausibleHTTPClient

    await connector.breakdown(
        site_id="example.com",
        period="7d",
        property="event:page",
        page=3,
        limit=25,
    )
    mock_PlausibleHTTPClient.get_breakdown.assert_awaited_once()
    kwargs = mock_PlausibleHTTPClient.get_breakdown.await_args.kwargs
    assert kwargs["site_id"] == "example.com"
    assert kwargs["period"] == "7d"
    assert kwargs["property"] == "event:page"
    assert kwargs["page"] == 3
    assert kwargs["limit"] == 25


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert PlausibleConnector.CONNECTOR_TYPE == "plausible"


def test_auth_type_class_attr():
    assert PlausibleConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(PlausibleConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in PlausibleConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert 401 in PlausibleConnector._STATUS_MAP
    assert 403 in PlausibleConnector._STATUS_MAP
    assert 429 in PlausibleConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = PlausibleConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = PlausibleConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
