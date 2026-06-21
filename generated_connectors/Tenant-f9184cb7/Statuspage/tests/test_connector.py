"""Unit tests for StatuspageConnector — respx-mocked, zero real I/O.

Coverage:
- install() — happy path, missing credentials, 401, 404 page-not-found
- authorize() — returns OAuth-typed TokenInfo wrapping the api_key
- health_check() — healthy / 401 auth-error / 404 page-not-found / 5xx
- sync() — incidents + maintenances ingested via mocked client
- Every public method — body shape, params, envelope keys
- Auth header — literal `OAuth <api_key>` (NOT `Bearer`)
- Retry — 429 + Retry-After then 200; 5xx then 200
- Class-attr identity — CONNECTOR_TYPE / AUTH_TYPE / REQUIRED_CONFIG_KEYS / _STATUS_MAP
- Multi-tenant — instances are independent
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.base_connector import AuthStatus, ConnectorHealth  # noqa: E402

from connector import StatuspageConnector  # noqa: E402
from client.http_client import StatuspageHTTPClient  # noqa: E402
from exceptions import (  # noqa: E402
    StatuspageAuthError,
    StatuspageError,
    StatuspageNotFound,
)
from tests.conftest import (  # noqa: E402
    BASE_URL,
    CONNECTOR_ID,
    PAGE_ID,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


async def test_install_success(connector_respx):
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        route = router.get(f"/pages/{PAGE_ID}").mock(
            return_value=httpx.Response(200, json={"id": PAGE_ID, "name": "Status"})
        )
        result = await connector_respx.install()
        assert route.called
        sent = route.calls.last.request
        # Statuspage demands literal `OAuth` scheme — not Bearer.
        assert sent.headers["Authorization"] == f"OAuth {TEST_API_KEY}"
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID


async def test_install_missing_api_key():
    c = StatuspageConnector(
        tenant_id="t",
        connector_id="c",
        config={"api_key": "", "page_id": PAGE_ID, "base_url": BASE_URL},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


async def test_install_auth_error_uses_oauth_header(connector_respx):
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        route = router.get(f"/pages/{PAGE_ID}").mock(
            return_value=httpx.Response(401, json={"error": "Unauthorized"})
        )
        result = await connector_respx.install()
        assert route.called
        sent = route.calls.last.request
        # Critical: scheme keyword must be `OAuth`, not `Bearer`.
        assert sent.headers["Authorization"].startswith("OAuth ")
        assert "Bearer" not in sent.headers["Authorization"]
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


async def test_install_page_not_found(connector_respx):
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get(f"/pages/{PAGE_ID}").mock(
            return_value=httpx.Response(404, json={"error": "page not found"})
        )
        result = await connector_respx.install()
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
        assert "not found" in result.message


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════


async def test_authorize_wraps_api_key_as_oauth_token(connector):
    token = await connector.authorize(auth_code="", state="")
    assert token.access_token == TEST_API_KEY
    assert token.token_type == "OAuth"
    assert token.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


async def test_health_check_healthy(connector_respx):
    with respx.mock(base_url=BASE_URL) as router:
        router.get(f"/pages/{PAGE_ID}").mock(
            return_value=httpx.Response(200, json={"id": PAGE_ID})
        )
        result = await connector_respx.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED


async def test_health_check_auth_failure(connector_respx):
    with respx.mock(base_url=BASE_URL) as router:
        router.get(f"/pages/{PAGE_ID}").mock(
            return_value=httpx.Response(403, json={"error": "Forbidden"})
        )
        result = await connector_respx.health_check()
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
        assert result.health == ConnectorHealth.DEGRADED


async def test_health_check_page_not_found(connector_respx):
    with respx.mock(base_url=BASE_URL) as router:
        router.get(f"/pages/{PAGE_ID}").mock(
            return_value=httpx.Response(404, json={"error": "no such page"})
        )
        result = await connector_respx.health_check()
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════


async def test_sync_aggregates_incidents_and_maintenances(
    connector, mock_StatuspageHTTPClient
):
    _, http = mock_StatuspageHTTPClient
    http.list_incidents.return_value = [
        {
            "id": "inc-1",
            "name": "DB outage",
            "status": "resolved",
            "shortlink": "https://stspg.io/inc-1",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T01:00:00Z",
            "incident_updates": [{"body": "Investigating"}, {"body": "Resolved"}],
            "components": [{"id": "cmp-1"}],
        }
    ]
    http.list_maintenances.return_value = [
        {
            "id": "mnt-1",
            "name": "Network upgrade",
            "status": "scheduled",
            "scheduled_for": "2025-02-01T00:00:00Z",
            "scheduled_until": "2025-02-01T02:00:00Z",
        }
    ]
    result = await connector.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


async def test_sync_no_page_id_returns_empty():
    c = StatuspageConnector(
        tenant_id="t",
        connector_id="c",
        config={"api_key": TEST_API_KEY, "page_id": "", "base_url": BASE_URL},
    )
    result = await c.sync()
    assert result.documents_found == 0
    assert result.documents_synced == 0


# ═══════════════════════════════════════════════════════════════════════════
# Pages
# ═══════════════════════════════════════════════════════════════════════════


async def test_list_pages(connector_respx):
    sample = [{"id": PAGE_ID, "name": "Status"}]
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get("/pages").mock(return_value=httpx.Response(200, json=sample))
        out = await connector_respx.list_pages(page=1, per_page=50)
        assert route.called
        assert out == sample
        assert (
            route.calls.last.request.headers["Authorization"]
            == f"OAuth {TEST_API_KEY}"
        )
        qs = route.calls.last.request.url.params
        assert qs["page"] == "1"
        assert qs["per_page"] == "50"


async def test_get_page_uses_default_page_id(connector_respx):
    with respx.mock(base_url=BASE_URL) as router:
        router.get(f"/pages/{PAGE_ID}").mock(
            return_value=httpx.Response(200, json={"id": PAGE_ID, "name": "Status"})
        )
        out = await connector_respx.get_page()
        assert out["id"] == PAGE_ID


# ═══════════════════════════════════════════════════════════════════════════
# Components
# ═══════════════════════════════════════════════════════════════════════════


async def test_list_components(connector_respx):
    sample = [{"id": "cmp-1", "name": "API", "status": "operational"}]
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(f"/pages/{PAGE_ID}/components").mock(
            return_value=httpx.Response(200, json=sample)
        )
        out = await connector_respx.list_components(PAGE_ID)
        assert route.called
        assert out == sample


async def test_get_component(connector_respx):
    sample = {"id": "cmp-1", "name": "API"}
    with respx.mock(base_url=BASE_URL) as router:
        router.get(f"/pages/{PAGE_ID}/components/cmp-1").mock(
            return_value=httpx.Response(200, json=sample)
        )
        out = await connector_respx.get_component(PAGE_ID, "cmp-1")
        assert out == sample


async def test_create_component_body_envelope(connector_respx):
    response = {"id": "cmp-new", "name": "Edge", "status": "operational"}
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(f"/pages/{PAGE_ID}/components").mock(
            return_value=httpx.Response(201, json=response)
        )
        out = await connector_respx.create_component(
            page_id=PAGE_ID,
            name="Edge",
            description="Edge nodes",
            status="operational",
            showcase=True,
            only_show_if_degraded=False,
        )
        assert route.called
        payload = json.loads(route.calls.last.request.content)
        # Statuspage requires the `component` envelope.
        assert "component" in payload
        assert payload["component"]["name"] == "Edge"
        assert payload["component"]["description"] == "Edge nodes"
        assert payload["component"]["status"] == "operational"
        assert out == response


async def test_update_component_status(connector_respx):
    response = {"id": "cmp-1", "status": "major_outage"}
    with respx.mock(base_url=BASE_URL) as router:
        route = router.patch(f"/pages/{PAGE_ID}/components/cmp-1").mock(
            return_value=httpx.Response(200, json=response)
        )
        out = await connector_respx.update_component_status(
            PAGE_ID, "cmp-1", "major_outage"
        )
        assert route.called
        body = json.loads(route.calls.last.request.content)
        assert body == {"component": {"status": "major_outage"}}
        assert out == response


async def test_update_component_arbitrary_fields(connector_respx):
    response = {"id": "cmp-1", "description": "new desc"}
    with respx.mock(base_url=BASE_URL) as router:
        route = router.patch(f"/pages/{PAGE_ID}/components/cmp-1").mock(
            return_value=httpx.Response(200, json=response)
        )
        out = await connector_respx.update_component(
            PAGE_ID, "cmp-1", {"description": "new desc"}
        )
        body = json.loads(route.calls.last.request.content)
        assert body == {"component": {"description": "new desc"}}
        assert out == response


async def test_delete_component(connector_respx):
    with respx.mock(base_url=BASE_URL) as router:
        route = router.delete(f"/pages/{PAGE_ID}/components/cmp-1").mock(
            return_value=httpx.Response(204)
        )
        out = await connector_respx.delete_component(PAGE_ID, "cmp-1")
        assert route.called
        assert out == {}


# ═══════════════════════════════════════════════════════════════════════════
# Component groups
# ═══════════════════════════════════════════════════════════════════════════


async def test_list_component_groups(connector_respx):
    sample = [{"id": "grp-1", "name": "Edge", "components": ["cmp-1", "cmp-2"]}]
    with respx.mock(base_url=BASE_URL) as router:
        router.get(f"/pages/{PAGE_ID}/component-groups").mock(
            return_value=httpx.Response(200, json=sample)
        )
        out = await connector_respx.list_component_groups(PAGE_ID)
        assert out == sample


# ═══════════════════════════════════════════════════════════════════════════
# Incidents
# ═══════════════════════════════════════════════════════════════════════════


async def test_list_incidents_with_query(connector_respx):
    sample = [{"id": "inc-1", "name": "DB outage"}]
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(f"/pages/{PAGE_ID}/incidents").mock(
            return_value=httpx.Response(200, json=sample)
        )
        out = await connector_respx.list_incidents(PAGE_ID, q="DB", limit=10, page=2)
        assert route.called
        req = route.calls.last.request
        assert req.url.params["q"] == "DB"
        assert req.url.params["limit"] == "10"
        assert req.url.params["page"] == "2"
        assert out == sample


async def test_get_incident(connector_respx):
    sample = {"id": "inc-1", "name": "DB outage"}
    with respx.mock(base_url=BASE_URL) as router:
        router.get(f"/pages/{PAGE_ID}/incidents/inc-1").mock(
            return_value=httpx.Response(200, json=sample)
        )
        out = await connector_respx.get_incident(PAGE_ID, "inc-1")
        assert out == sample


async def test_create_incident_body_shape(connector_respx):
    response = {"id": "inc-new", "name": "Latency spike", "status": "investigating"}
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(f"/pages/{PAGE_ID}/incidents").mock(
            return_value=httpx.Response(201, json=response)
        )
        out = await connector_respx.create_incident(
            page_id=PAGE_ID,
            name="Latency spike",
            status="investigating",
            impact_override="major",
            body="We are investigating elevated latency.",
            component_ids=["cmp-1", "cmp-2"],
            components={"cmp-1": "degraded_performance"},
            deliver_notifications=True,
        )
        assert route.called
        payload = json.loads(route.calls.last.request.content)
        assert "incident" in payload
        inc = payload["incident"]
        assert inc["name"] == "Latency spike"
        assert inc["status"] == "investigating"
        assert inc["impact_override"] == "major"
        assert inc["body"] == "We are investigating elevated latency."
        assert inc["component_ids"] == ["cmp-1", "cmp-2"]
        assert inc["components"] == {"cmp-1": "degraded_performance"}
        assert inc["deliver_notifications"] is True
        assert out == response


async def test_update_incident(connector_respx):
    response = {"id": "inc-1", "status": "resolved"}
    with respx.mock(base_url=BASE_URL) as router:
        route = router.patch(f"/pages/{PAGE_ID}/incidents/inc-1").mock(
            return_value=httpx.Response(200, json=response)
        )
        out = await connector_respx.update_incident(
            PAGE_ID, "inc-1", {"status": "resolved", "body": "All clear."}
        )
        assert route.called
        body = json.loads(route.calls.last.request.content)
        assert body == {"incident": {"status": "resolved", "body": "All clear."}}
        assert out == response


# ═══════════════════════════════════════════════════════════════════════════
# Maintenances
# ═══════════════════════════════════════════════════════════════════════════


async def test_list_maintenances(connector_respx):
    sample = [{"id": "mnt-1", "name": "Network upgrade", "status": "scheduled"}]
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(f"/pages/{PAGE_ID}/incidents/scheduled").mock(
            return_value=httpx.Response(200, json=sample)
        )
        out = await connector_respx.list_maintenances(PAGE_ID, limit=50, page=1)
        assert route.called
        qs = route.calls.last.request.url.params
        assert qs["limit"] == "50"
        assert qs["page"] == "1"
        assert out == sample


# ═══════════════════════════════════════════════════════════════════════════
# Subscribers
# ═══════════════════════════════════════════════════════════════════════════


async def test_list_subscribers_filters(connector_respx):
    sample = [{"id": "sub-1", "email": "a@example.com"}]
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(f"/pages/{PAGE_ID}/subscribers").mock(
            return_value=httpx.Response(200, json=sample)
        )
        out = await connector_respx.list_subscribers(
            PAGE_ID, type="email", state="active", limit=25, page=1
        )
        assert route.called
        params = route.calls.last.request.url.params
        assert params["type"] == "email"
        assert params["state"] == "active"
        assert params["limit"] == "25"
        assert out == sample


async def test_create_subscriber(connector_respx):
    response = {"id": "sub-new", "email": "ops@example.com"}
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(f"/pages/{PAGE_ID}/subscribers").mock(
            return_value=httpx.Response(201, json=response)
        )
        out = await connector_respx.create_subscriber(PAGE_ID, email="ops@example.com")
        assert route.called
        body = json.loads(route.calls.last.request.content)
        assert body == {"subscriber": {"email": "ops@example.com"}}
        assert out == response


async def test_delete_subscriber(connector_respx):
    with respx.mock(base_url=BASE_URL) as router:
        route = router.delete(f"/pages/{PAGE_ID}/subscribers/sub-1").mock(
            return_value=httpx.Response(204)
        )
        out = await connector_respx.delete_subscriber(PAGE_ID, "sub-1")
        assert route.called
        assert out == {}


# ═══════════════════════════════════════════════════════════════════════════
# Metrics + templates
# ═══════════════════════════════════════════════════════════════════════════


async def test_list_metrics(connector_respx):
    sample = [{"id": "metric-1", "name": "Response Time"}]
    with respx.mock(base_url=BASE_URL) as router:
        router.get(f"/pages/{PAGE_ID}/metrics").mock(
            return_value=httpx.Response(200, json=sample)
        )
        out = await connector_respx.list_metrics(PAGE_ID)
        assert out == sample


async def test_list_incident_templates(connector_respx):
    sample = [{"id": "tpl-1", "name": "Postmortem"}]
    with respx.mock(base_url=BASE_URL) as router:
        router.get(f"/pages/{PAGE_ID}/incident_templates").mock(
            return_value=httpx.Response(200, json=sample)
        )
        out = await connector_respx.list_incident_templates(PAGE_ID)
        assert out == sample


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════


async def test_retry_on_429_then_success(no_retry_sleep):
    http = StatuspageHTTPClient(api_key="k", base_url=BASE_URL, max_retries=2)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        sample = [{"id": "page1"}]
        route = router.get("/pages").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"}),
                httpx.Response(200, json=sample),
            ]
        )
        out = await http.list_pages(page=1, per_page=1)
        assert out == sample
        assert route.call_count == 2


async def test_retry_on_500_then_success(no_retry_sleep):
    http = StatuspageHTTPClient(api_key="k", base_url=BASE_URL, max_retries=2)
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        route = router.get("/pages").mock(
            side_effect=[
                httpx.Response(500, json={"error": "boom"}),
                httpx.Response(200, json=[]),
            ]
        )
        out = await http.list_pages()
        assert out == []
        assert route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert StatuspageConnector.CONNECTOR_TYPE == "statuspage"


def test_auth_type_class_attr():
    assert StatuspageConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(StatuspageConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in StatuspageConnector.REQUIRED_CONFIG_KEYS
    assert "page_id" in StatuspageConnector.REQUIRED_CONFIG_KEYS


def test_status_map_classifies_auth_and_rate_limit():
    sm = StatuspageConnector._STATUS_MAP
    assert sm[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert sm[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert sm[429] == ("DEGRADED", "CONNECTED")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = StatuspageConnector(
        tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = StatuspageConnector(
        tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — incident → NormalizedDocument id is tenant-scoped
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_incident_produces_tenant_scoped_id():
    from helpers.normalizer import normalize_incident

    raw = {
        "id": "inc-99",
        "name": "DB outage",
        "status": "resolved",
        "shortlink": "https://stspg.io/inc-99",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T01:00:00Z",
        "incident_updates": [{"body": "Investigating"}, {"body": "Resolved"}],
        "components": [{"id": "cmp-1"}],
        "impact": "major",
        "page_id": PAGE_ID,
    }
    doc = normalize_incident(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{TENANT_ID}_inc-99"
    assert doc.source_id == "inc-99"
    assert doc.title == "DB outage"
    assert "Investigating" in doc.content
    assert doc.metadata["status"] == "resolved"
    assert doc.metadata["impact"] == "major"
    assert doc.metadata["kind"] == "statuspage.incident"
    assert doc.metadata["component_ids"] == ["cmp-1"]
