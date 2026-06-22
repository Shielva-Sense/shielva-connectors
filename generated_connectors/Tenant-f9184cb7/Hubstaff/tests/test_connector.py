"""Unit tests for HubstaffConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import HubstaffConnector
from exceptions import HubstaffAuthError, HubstaffError, HubstaffNotFound

from tests.conftest import (
    CONNECTOR_ID,
    HUBSTAFF_BASE,
    TENANT_ID,
    TEST_ACCESS_TOKEN,
    TEST_CONFIG,
    TEST_ORG_ID,
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
async def test_install_missing_access_token(connector):
    connector.config.pop("access_token", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer_pat(connector):
    """Connector must send the access_token as 'Bearer <token>' in Authorization."""
    route = respx.get(f"{HUBSTAFF_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"user": {"id": 1}})
    )
    await connector.get_current_user()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_ACCESS_TOKEN}"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_hubstaff_auth_error(connector):
    respx.get(f"{HUBSTAFF_BASE}/users/me").mock(
        return_value=httpx.Response(401, json={"error": "Invalid token"})
    )
    with pytest.raises(HubstaffAuthError):
        await connector.get_current_user()


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{HUBSTAFF_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"user": {"id": 1, "email": "u@x"}})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{HUBSTAFF_BASE}/users/me").mock(
        return_value=httpx.Response(401, json={"error": "Invalid token"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Identity
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_current_user_success(connector):
    respx.get(f"{HUBSTAFF_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"user": {"id": 7}})
    )
    result = await connector.get_current_user()
    assert result["user"]["id"] == 7


@respx.mock
@pytest.mark.asyncio
async def test_get_user_not_found(connector):
    respx.get(f"{HUBSTAFF_BASE}/users/999").mock(
        return_value=httpx.Response(404, json={"error": "user not found"})
    )
    with pytest.raises(HubstaffNotFound):
        await connector.get_user(999)


# ═══════════════════════════════════════════════════════════════════════════
# Organizations
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_organizations_success(connector):
    orgs_resp = {
        "organizations": [{"id": 1001, "name": "Acme"}],
        "pagination": {},
    }
    route = respx.get(f"{HUBSTAFF_BASE}/organizations").mock(
        return_value=httpx.Response(200, json=orgs_resp)
    )
    result = await connector.list_organizations(page_limit=10)
    assert route.called
    assert result["organizations"][0]["id"] == 1001
    qs = route.calls[0].request.url.params
    assert qs.get("page_limit") == "10"


# ═══════════════════════════════════════════════════════════════════════════
# Users / Teams
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_users_success(connector):
    respx.get(f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/members").mock(
        return_value=httpx.Response(200, json={"members": [{"id": 42}]})
    )
    result = await connector.list_users(TEST_ORG_ID)
    assert result["members"][0]["id"] == 42


@respx.mock
@pytest.mark.asyncio
async def test_list_teams_success(connector):
    respx.get(f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/teams").mock(
        return_value=httpx.Response(200, json={"teams": [{"id": 11, "name": "T"}]})
    )
    result = await connector.list_teams(TEST_ORG_ID)
    assert result["teams"][0]["id"] == 11


# ═══════════════════════════════════════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_projects_with_status(connector):
    route = respx.get(
        f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/projects"
    ).mock(return_value=httpx.Response(200, json={"projects": [{"id": 1}]}))
    result = await connector.list_projects(
        TEST_ORG_ID, status="archived", page_limit=20
    )
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("status") == "archived"
    assert qs.get("page_limit") == "20"
    assert result["projects"][0]["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_get_project_success(connector):
    respx.get(f"{HUBSTAFF_BASE}/projects/42").mock(
        return_value=httpx.Response(200, json={"project": {"id": 42, "name": "P"}})
    )
    result = await connector.get_project(42)
    assert result["project"]["id"] == 42


@respx.mock
@pytest.mark.asyncio
async def test_create_project_posts_envelope(connector):
    route = respx.post(
        f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/projects"
    ).mock(return_value=httpx.Response(200, json={"project": {"id": 77}}))
    result = await connector.create_project(
        TEST_ORG_ID, name="New Project", description="desc"
    )
    import json as _json

    body = _json.loads(route.calls[0].request.content.decode())
    assert body == {"project": {"name": "New Project", "description": "desc"}}
    assert result["project"]["id"] == 77


# ═══════════════════════════════════════════════════════════════════════════
# Tasks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_tasks_status_param(connector):
    route = respx.get(f"{HUBSTAFF_BASE}/projects/42/tasks").mock(
        return_value=httpx.Response(200, json={"tasks": [{"id": 5, "summary": "S"}]})
    )
    result = await connector.list_tasks(42, status="closed")
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("status") == "closed"
    assert result["tasks"][0]["id"] == 5


# ═══════════════════════════════════════════════════════════════════════════
# Activities / Time Entries / Daily Activities
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_activities_passes_filters(connector):
    route = respx.get(
        f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/activities"
    ).mock(return_value=httpx.Response(200, json={"activities": [{"id": 1}]}))
    await connector.list_activities(
        TEST_ORG_ID,
        date_start="2026-01-01",
        date_stop="2026-01-02",
        user_ids=[1, 2],
        project_ids=[10],
    )
    qs = route.calls[0].request.url.params
    assert qs.get("date_start") == "2026-01-01"
    assert qs.get("date_stop") == "2026-01-02"
    assert qs.get("user_ids") == "1,2"
    assert qs.get("project_ids") == "10"


@respx.mock
@pytest.mark.asyncio
async def test_list_time_entries_success(connector):
    respx.get(
        f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/time_entries"
    ).mock(return_value=httpx.Response(200, json={"time_entries": [{"id": 9}]}))
    result = await connector.list_time_entries(
        TEST_ORG_ID, date_start="2026-01-01", date_stop="2026-01-02"
    )
    assert result["time_entries"][0]["id"] == 9


@respx.mock
@pytest.mark.asyncio
async def test_list_daily_activities_success(connector):
    respx.get(
        f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/activities/daily"
    ).mock(
        return_value=httpx.Response(
            200, json={"daily_activities": [{"id": 1, "tracked": 3600}]}
        )
    )
    result = await connector.list_daily_activities(
        TEST_ORG_ID, date_start="2026-01-01", date_stop="2026-01-02"
    )
    assert result["daily_activities"][0]["tracked"] == 3600


# ═══════════════════════════════════════════════════════════════════════════
# Screenshots / Apps / URLs / Notes
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_screenshots_success(connector):
    respx.get(
        f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/screenshots"
    ).mock(return_value=httpx.Response(200, json={"screenshots": [{"id": "s1"}]}))
    result = await connector.list_screenshots(TEST_ORG_ID)
    assert result["screenshots"][0]["id"] == "s1"


@respx.mock
@pytest.mark.asyncio
async def test_list_apps_success(connector):
    respx.get(
        f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/application_activities"
    ).mock(return_value=httpx.Response(200, json={"application_activities": [{"id": 2}]}))
    result = await connector.list_apps(TEST_ORG_ID)
    assert result["application_activities"][0]["id"] == 2


@respx.mock
@pytest.mark.asyncio
async def test_list_urls_success(connector):
    respx.get(
        f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/url_activities"
    ).mock(return_value=httpx.Response(200, json={"url_activities": [{"id": 3}]}))
    result = await connector.list_urls(TEST_ORG_ID)
    assert result["url_activities"][0]["id"] == 3


@respx.mock
@pytest.mark.asyncio
async def test_list_notes_success(connector):
    respx.get(
        f"{HUBSTAFF_BASE}/organizations/{TEST_ORG_ID}/notes"
    ).mock(return_value=httpx.Response(200, json={"notes": [{"id": 4}]}))
    result = await connector.list_notes(TEST_ORG_ID)
    assert result["notes"][0]["id"] == 4


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{HUBSTAFF_BASE}/users/me").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json={"user": {"id": "after-retry"}}),
        ]
    )
    result = await connector.get_current_user()
    assert route.call_count == 2
    assert result["user"]["id"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{HUBSTAFF_BASE}/users/me").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json={"user": {"id": 1}}),
        ]
    )
    result = await connector.get_current_user()
    assert route.call_count == 2
    assert result == {"user": {"id": 1}}


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert HubstaffConnector.CONNECTOR_TYPE == "hubstaff"


def test_auth_type_class_attr():
    assert HubstaffConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(HubstaffConnector, "REQUIRED_CONFIG_KEYS")
    assert "access_token" in HubstaffConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(HubstaffConnector, "_STATUS_MAP")
    assert 401 in HubstaffConnector._STATUS_MAP
    assert 403 in HubstaffConnector._STATUS_MAP
    assert 429 in HubstaffConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = HubstaffConnector(
        tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = HubstaffConnector(
        tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
