"""Unit tests for ClockifyConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import ClockifyConnector
from exceptions import (
    ClockifyAuthError,
    ClockifyError,
    ClockifyNotFound,
    ClockifyRateLimitError,
)
from helpers.utils import RETRY_DELAY_S  # noqa: F401  (imported for symmetry)

from tests.conftest import (
    API_BASE,
    API_KEY,
    CONNECTOR_ID,
    REPORTS_BASE,
    TENANT_ID,
    TEST_CONFIG,
    USER_ID,
    WORKSPACE_ID,
)


# ═══════════════════════════════════════════════════════════════════════════
# Identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type():
    assert ClockifyConnector.CONNECTOR_TYPE == "clockify"


def test_auth_type():
    assert ClockifyConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(ClockifyConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in ClockifyConnector.REQUIRED_CONFIG_KEYS


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


# ═══════════════════════════════════════════════════════════════════════════
# health_check() + auth error
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{API_BASE}/user").mock(
        return_value=httpx.Response(200, json={"id": USER_ID, "email": "u@example.com"})
    )
    status = await connector.health_check()
    assert status.health == ConnectorHealth.HEALTHY
    assert status.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error_401(connector):
    respx.get(f"{API_BASE}/user").mock(
        return_value=httpx.Response(401, json={"message": "Api key required"})
    )
    status = await connector.health_check()
    assert status.auth_status == AuthStatus.TOKEN_EXPIRED
    assert status.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# get_current_user()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_current_user(connector):
    respx.get(f"{API_BASE}/user").mock(
        return_value=httpx.Response(
            200,
            json={"id": USER_ID, "email": "user@example.com", "name": "User"},
        )
    )
    me = await connector.get_current_user()
    assert me["id"] == USER_ID
    assert me["email"] == "user@example.com"


# ═══════════════════════════════════════════════════════════════════════════
# list_workspaces()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_workspaces(connector):
    respx.get(f"{API_BASE}/workspaces").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": WORKSPACE_ID, "name": "Personal"},
                {"id": "ws_two", "name": "Acme Co"},
            ],
        )
    )
    workspaces = await connector.list_workspaces()
    assert len(workspaces) == 2
    assert workspaces[0]["id"] == WORKSPACE_ID


# ═══════════════════════════════════════════════════════════════════════════
# list_projects() + name filter
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_projects_with_name_filter(connector):
    route = respx.get(f"{API_BASE}/workspaces/{WORKSPACE_ID}/projects").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "p1", "name": "Backend", "workspaceId": WORKSPACE_ID}],
        )
    )
    result = await connector.list_projects(
        workspace_id=WORKSPACE_ID, name="Back", page=1, page_size=25
    )
    assert len(result) == 1
    assert result[0]["name"] == "Backend"
    # confirm query params
    call = route.calls.last
    assert call.request.url.params["name"] == "Back"
    assert call.request.url.params["page"] == "1"
    assert call.request.url.params["page-size"] == "25"
    assert call.request.url.params["archived"] == "false"
    # X-Api-Key header
    assert call.request.headers["X-Api-Key"] == API_KEY


# ═══════════════════════════════════════════════════════════════════════════
# create_project() — billable + hourly_rate
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_create_project_billable_with_hourly_rate(connector):
    route = respx.post(f"{API_BASE}/workspaces/{WORKSPACE_ID}/projects").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "p_new",
                "name": "Voice",
                "workspaceId": WORKSPACE_ID,
                "billable": True,
                "hourlyRate": {"amount": 12000, "currency": "USD"},
            },
        )
    )
    result = await connector.create_project(
        workspace_id=WORKSPACE_ID,
        name="Voice",
        billable=True,
        hourly_rate={"amount": 12000, "currency": "USD"},
    )
    assert result["id"] == "p_new"
    assert result["billable"] is True
    body = route.calls.last.request.read().decode()
    import json as _json
    parsed = _json.loads(body)
    assert parsed["name"] == "Voice"
    assert parsed["billable"] is True
    assert parsed["hourlyRate"] == {"amount": 12000, "currency": "USD"}


# ═══════════════════════════════════════════════════════════════════════════
# list_clients()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_clients(connector):
    respx.get(f"{API_BASE}/workspaces/{WORKSPACE_ID}/clients").mock(
        return_value=httpx.Response(
            200, json=[{"id": "c1", "name": "Acme", "workspaceId": WORKSPACE_ID}]
        )
    )
    result = await connector.list_clients(workspace_id=WORKSPACE_ID)
    assert len(result) == 1
    assert result[0]["name"] == "Acme"


# ═══════════════════════════════════════════════════════════════════════════
# create_client()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_create_client(connector):
    respx.post(f"{API_BASE}/workspaces/{WORKSPACE_ID}/clients").mock(
        return_value=httpx.Response(
            201,
            json={"id": "c_new", "name": "Shielva", "workspaceId": WORKSPACE_ID},
        )
    )
    result = await connector.create_client(
        workspace_id=WORKSPACE_ID, name="Shielva", email="ops@shielva.ai"
    )
    assert result["id"] == "c_new"
    assert result["name"] == "Shielva"


# ═══════════════════════════════════════════════════════════════════════════
# list_tasks()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_tasks(connector):
    project_id = "p1"
    route = respx.get(
        f"{API_BASE}/workspaces/{WORKSPACE_ID}/projects/{project_id}/tasks"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "t1", "name": "Plumbing", "status": "ACTIVE"},
                {"id": "t2", "name": "Wiring", "status": "ACTIVE"},
            ],
        )
    )
    result = await connector.list_tasks(
        workspace_id=WORKSPACE_ID, project_id=project_id
    )
    assert len(result) == 2
    assert route.calls.last.request.url.params["status"] == "ACTIVE"


# ═══════════════════════════════════════════════════════════════════════════
# list_time_entries() with date range
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_time_entries_with_date_range(connector):
    route = respx.get(
        f"{API_BASE}/workspaces/{WORKSPACE_ID}/user/{USER_ID}/time-entries"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "te1",
                    "workspaceId": WORKSPACE_ID,
                    "userId": USER_ID,
                    "description": "Coding",
                    "timeInterval": {
                        "start": "2026-06-20T09:00:00Z",
                        "end": "2026-06-20T10:00:00Z",
                        "duration": "PT1H",
                    },
                    "billable": True,
                    "projectId": "p1",
                }
            ],
        )
    )
    result = await connector.list_time_entries(
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        start="2026-06-20T00:00:00Z",
        end="2026-06-21T00:00:00Z",
    )
    assert len(result) == 1
    params = route.calls.last.request.url.params
    assert params["start"] == "2026-06-20T00:00:00Z"
    assert params["end"] == "2026-06-21T00:00:00Z"


# ═══════════════════════════════════════════════════════════════════════════
# create_time_entry()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_create_time_entry(connector):
    route = respx.post(
        f"{API_BASE}/workspaces/{WORKSPACE_ID}/time-entries"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "te_new",
                "workspaceId": WORKSPACE_ID,
                "userId": USER_ID,
                "description": "Design review",
            },
        )
    )
    result = await connector.create_time_entry(
        workspace_id=WORKSPACE_ID,
        start="2026-06-21T10:00:00Z",
        end="2026-06-21T11:00:00Z",
        description="Design review",
        project_id="p1",
        billable=True,
        tag_ids=["tg1"],
    )
    assert result["id"] == "te_new"
    import json as _json
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["start"] == "2026-06-21T10:00:00Z"
    assert body["end"] == "2026-06-21T11:00:00Z"
    assert body["projectId"] == "p1"
    assert body["billable"] is True
    assert body["tagIds"] == ["tg1"]


# ═══════════════════════════════════════════════════════════════════════════
# update_time_entry()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_update_time_entry(connector):
    entry_id = "te_existing"
    route = respx.put(
        f"{API_BASE}/workspaces/{WORKSPACE_ID}/time-entries/{entry_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": entry_id, "description": "Updated"},
        )
    )
    result = await connector.update_time_entry(
        workspace_id=WORKSPACE_ID,
        entry_id=entry_id,
        fields={"description": "Updated", "billable": False},
    )
    assert result["id"] == entry_id
    import json as _json
    body = _json.loads(route.calls.last.request.read().decode())
    assert body == {"description": "Updated", "billable": False}


# ═══════════════════════════════════════════════════════════════════════════
# delete_time_entry()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_delete_time_entry(connector):
    entry_id = "te_to_delete"
    route = respx.delete(
        f"{API_BASE}/workspaces/{WORKSPACE_ID}/time-entries/{entry_id}"
    ).mock(return_value=httpx.Response(204))
    result = await connector.delete_time_entry(
        workspace_id=WORKSPACE_ID, entry_id=entry_id
    )
    assert result == {}
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# summary_report() — verifies REPORTS base URL
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_summary_report_uses_reports_base_url(connector):
    route = respx.post(
        f"{REPORTS_BASE}/workspaces/{WORKSPACE_ID}/reports/summary"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "totals": [{"totalTime": 3600, "totalBillableTime": 1800}],
                "groupOne": [],
            },
        )
    )
    result = await connector.summary_report(
        workspace_id=WORKSPACE_ID,
        date_range_start="2026-06-01T00:00:00Z",
        date_range_end="2026-06-30T23:59:59Z",
        summary_filter={"groups": ["PROJECT", "USER"]},
    )
    assert "totals" in result
    # Verify the host is the Reports host, NOT the standard API host
    sent_url = str(route.calls.last.request.url)
    assert "reports.api.clockify.me" in sent_url
    assert "api.clockify.me/api/v1" not in sent_url


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — succeeds on the second attempt
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_succeeds(connector):
    route = respx.get(f"{API_BASE}/user").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow down"}, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": USER_ID}),
        ]
    )
    me = await connector.get_current_user()
    assert me["id"] == USER_ID
    assert route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# 404 → ClockifyNotFound
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_project_not_found(connector):
    respx.get(f"{API_BASE}/workspaces/{WORKSPACE_ID}/projects/missing").mock(
        return_value=httpx.Response(404, json={"message": "Project not found"})
    )
    with pytest.raises(ClockifyNotFound):
        await connector.get_project(WORKSPACE_ID, "missing")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_different_tenants_independent_instances():
    c1 = ClockifyConnector(
        tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = ClockifyConnector(
        tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Missing API key → ClockifyAuthError when calling helpers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_missing_api_key_raises_auth_error():
    cfg = dict(TEST_CONFIG)
    cfg.pop("api_key", None)
    connector = ClockifyConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg
    )
    with pytest.raises(ClockifyAuthError):
        await connector.get_current_user()


# ═══════════════════════════════════════════════════════════════════════════
# X-Api-Key header shape — never sent as Bearer / Authorization
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_x_api_key_header_used_not_bearer(connector):
    route = respx.get(f"{API_BASE}/user").mock(
        return_value=httpx.Response(200, json={"id": USER_ID})
    )
    await connector.get_current_user()
    headers = route.calls.last.request.headers
    assert headers.get("X-Api-Key") == API_KEY
    # Auth header MUST NOT be set — Clockify uses X-Api-Key exclusively.
    assert "authorization" not in {k.lower() for k in headers.keys()}


# ═══════════════════════════════════════════════════════════════════════════
# _STATUS_MAP class attr
# ═══════════════════════════════════════════════════════════════════════════

def test_status_map_defined():
    sm = ClockifyConnector._STATUS_MAP
    assert sm[401] == ("DEGRADED", "TOKEN_EXPIRED")
    assert sm[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert sm[429] == ("DEGRADED", "CONNECTED")


def test_required_config_keys_is_api_key_only():
    assert ClockifyConnector.REQUIRED_CONFIG_KEYS == ["api_key"]


# ═══════════════════════════════════════════════════════════════════════════
# get_time_entry()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_time_entry(connector):
    entry_id = "te_existing"
    respx.get(
        f"{API_BASE}/workspaces/{WORKSPACE_ID}/time-entries/{entry_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": entry_id, "description": "Refactor"},
        )
    )
    result = await connector.get_time_entry(WORKSPACE_ID, entry_id)
    assert result["id"] == entry_id


# ═══════════════════════════════════════════════════════════════════════════
# start_time_entry() — POST with no `end` field
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_start_time_entry_omits_end(connector):
    route = respx.post(
        f"{API_BASE}/workspaces/{WORKSPACE_ID}/time-entries"
    ).mock(
        return_value=httpx.Response(
            201,
            json={"id": "te_run", "workspaceId": WORKSPACE_ID, "userId": USER_ID},
        )
    )
    result = await connector.start_time_entry(
        workspace_id=WORKSPACE_ID,
        start="2026-06-21T12:00:00Z",
        description="Running timer",
        project_id="p1",
        billable=True,
    )
    assert result["id"] == "te_run"
    import json as _json
    body = _json.loads(route.calls.last.request.read().decode())
    assert body["start"] == "2026-06-21T12:00:00Z"
    assert body["description"] == "Running timer"
    assert body["projectId"] == "p1"
    assert body["billable"] is True
    # CRITICAL: running timer must NOT carry an `end` field
    assert "end" not in body


# ═══════════════════════════════════════════════════════════════════════════
# stop_time_entry() — PATCH user-scoped path
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_stop_time_entry(connector):
    route = respx.patch(
        f"{API_BASE}/workspaces/{WORKSPACE_ID}/user/{USER_ID}/time-entries"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": "te_run", "timeInterval": {"end": "2026-06-21T13:00:00Z"}},
        )
    )
    result = await connector.stop_time_entry(
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        end="2026-06-21T13:00:00Z",
    )
    assert result["id"] == "te_run"
    import json as _json
    body = _json.loads(route.calls.last.request.read().decode())
    assert body == {"end": "2026-06-21T13:00:00Z"}


# ═══════════════════════════════════════════════════════════════════════════
# list_users()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_users(connector):
    route = respx.get(f"{API_BASE}/workspaces/{WORKSPACE_ID}/users").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": USER_ID, "email": "user@example.com", "status": "ACTIVE"},
                {"id": "user_two", "email": "two@example.com", "status": "PENDING"},
            ],
        )
    )
    result = await connector.list_users(workspace_id=WORKSPACE_ID, status="ACTIVE")
    assert len(result) == 2
    params = route.calls.last.request.url.params
    assert params["status"] == "ACTIVE"
    assert params["page"] == "1"
    assert params["page-size"] == "50"


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — NormalizedDocument id is tenant-scoped
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_time_entry_id_is_tenant_scoped():
    from helpers.normalizer import normalize_time_entry

    raw = {
        "id": "te_abc",
        "workspaceId": WORKSPACE_ID,
        "userId": USER_ID,
        "description": "Pair programming",
        "billable": True,
        "projectId": "p_alpha",
        "tagIds": ["tg-1", "tg-2"],
        "timeInterval": {
            "start": "2026-06-21T10:00:00Z",
            "end": "2026-06-21T11:00:00Z",
            "duration": "PT1H",
        },
    }
    doc = normalize_time_entry(raw, CONNECTOR_ID, TENANT_ID)
    # tenant-scoped id is the canonical Shielva pattern: f"{tenant_id}_{source_id}"
    assert doc.id == f"{TENANT_ID}_te_abc"
    assert doc.source_id == "te_abc"
    assert doc.author == USER_ID
    assert doc.metadata["workspace_id"] == WORKSPACE_ID
    assert doc.metadata["project_id"] == "p_alpha"
    assert doc.metadata["billable"] is True
    assert doc.metadata["tag_ids"] == ["tg-1", "tg-2"]


# ═══════════════════════════════════════════════════════════════════════════
# 403 — INVALID_CREDENTIALS classification
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_403_classified_as_auth_error(connector):
    respx.get(f"{API_BASE}/user").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    status = await connector.health_check()
    # 403 surfaces as auth-failure in health_check (DEGRADED + TOKEN_EXPIRED via
    # ClockifyAuthError handler — see connector.health_check).
    assert status.auth_status == AuthStatus.TOKEN_EXPIRED
    assert status.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# 5xx surfaces as ClockifyError (not retried indefinitely)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_500_eventually_raises_clockify_error(connector):
    respx.get(f"{API_BASE}/user").mock(
        return_value=httpx.Response(500, json={"message": "Boom"})
    )
    with pytest.raises(ClockifyError):
        await connector.get_current_user()
