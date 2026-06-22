"""Unit tests for HarvestConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import HarvestConnector
from exceptions import (
    HarvestAuthError,
    HarvestBadRequestError,
    HarvestError,
    HarvestNotFound,
    HarvestRateLimitError,
)

from tests.conftest import (
    CONNECTOR_ID,
    HARVEST_BASE,
    TENANT_ID,
    TEST_ACCESS_TOKEN,
    TEST_ACCOUNT_ID,
    TEST_CONFIG,
    TEST_USER_AGENT,
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


@pytest.mark.asyncio
async def test_install_missing_account_id(connector):
    connector.config.pop("account_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer + Harvest-Account-Id + User-Agent)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_required_headers_sent_on_every_call(connector):
    """Connector must send Bearer auth + Harvest-Account-Id + User-Agent."""
    route = respx.get(f"{HARVEST_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    await connector.get_user_me()
    assert route.called
    headers = route.calls[0].request.headers
    assert headers.get("authorization") == f"Bearer {TEST_ACCESS_TOKEN}"
    assert headers.get("harvest-account-id") == TEST_ACCOUNT_ID
    assert headers.get("user-agent") == TEST_USER_AGENT


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_harvest_auth_error(connector):
    respx.get(f"{HARVEST_BASE}/users/me").mock(
        return_value=httpx.Response(401, json={"message": "Invalid PAT"})
    )
    with pytest.raises(HarvestAuthError):
        await connector.get_user_me()


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_403_raises_harvest_auth_error(connector):
    respx.get(f"{HARVEST_BASE}/users/me").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    with pytest.raises(HarvestAuthError):
        await connector.get_user_me()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{HARVEST_BASE}/users/me").mock(
        return_value=httpx.Response(
            200, json={"id": 99, "first_name": "Vivek", "email": "v@shielva.ai"}
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_token_expired(connector):
    respx.get(f"{HARVEST_BASE}/users/me").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_user_me_success(connector):
    respx.get(f"{HARVEST_BASE}/users/me").mock(
        return_value=httpx.Response(
            200, json={"id": 1, "first_name": "Vivek", "email": "v@shielva.ai"}
        )
    )
    result = await connector.get_user_me()
    assert result["email"] == "v@shielva.ai"


@respx.mock
@pytest.mark.asyncio
async def test_list_users_success(connector):
    route = respx.get(f"{HARVEST_BASE}/users").mock(
        return_value=httpx.Response(
            200, json={"users": [{"id": 1, "first_name": "Vivek"}], "next_page": None}
        )
    )
    result = await connector.list_users(is_active=True, page=1, per_page=50)
    assert route.called
    assert result["users"][0]["id"] == 1
    qs = route.calls[0].request.url.params
    assert qs.get("page") == "1"
    assert qs.get("per_page") == "50"
    assert qs.get("is_active", "").lower() == "true"


# ═══════════════════════════════════════════════════════════════════════════
# Clients
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_clients_success(connector):
    respx.get(f"{HARVEST_BASE}/clients").mock(
        return_value=httpx.Response(
            200,
            json={
                "clients": [
                    {"id": 1, "name": "Acme", "is_active": True, "currency": "USD"}
                ],
                "per_page": 100,
                "total_pages": 1,
                "total_entries": 1,
                "next_page": None,
                "page": 1,
            },
        )
    )
    result = await connector.list_clients(is_active=True, page=1, per_page=100)
    assert result["clients"][0]["name"] == "Acme"


# ═══════════════════════════════════════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_projects_with_client_filter(connector):
    route = respx.get(f"{HARVEST_BASE}/projects").mock(
        return_value=httpx.Response(
            200,
            json={
                "projects": [
                    {"id": 10, "name": "Website", "client": {"id": 1, "name": "Acme"}}
                ],
                "next_page": None,
            },
        )
    )
    result = await connector.list_projects(client_id=1)
    assert result["projects"][0]["id"] == 10
    qs = route.calls[0].request.url.params
    assert qs.get("client_id") == "1"


@respx.mock
@pytest.mark.asyncio
async def test_get_project_success(connector):
    pid = 555
    respx.get(f"{HARVEST_BASE}/projects/{pid}").mock(
        return_value=httpx.Response(200, json={"id": pid, "name": "Big Site"})
    )
    result = await connector.get_project(pid)
    assert result["id"] == pid


# ═══════════════════════════════════════════════════════════════════════════
# Tasks
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_tasks_success(connector):
    respx.get(f"{HARVEST_BASE}/tasks").mock(
        return_value=httpx.Response(
            200,
            json={"tasks": [{"id": 7, "name": "Design"}], "next_page": None},
        )
    )
    result = await connector.list_tasks(is_active=True)
    assert result["tasks"][0]["name"] == "Design"


# ═══════════════════════════════════════════════════════════════════════════
# Time entries — list, get, create, update, delete
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_time_entries_with_date_range(connector):
    route = respx.get(f"{HARVEST_BASE}/time_entries").mock(
        return_value=httpx.Response(
            200,
            json={
                "time_entries": [
                    {
                        "id": 555,
                        "spent_date": "2026-06-01",
                        "hours": 4.5,
                        "project": {"id": 10, "name": "Website"},
                        "task": {"id": 7, "name": "Design"},
                        "user": {"id": 1, "name": "Vivek"},
                    }
                ],
                "next_page": None,
            },
        )
    )
    result = await connector.list_time_entries(
        from_date="2026-06-01", to_date="2026-06-30"
    )
    assert result["time_entries"][0]["id"] == 555
    qs = route.calls[0].request.url.params
    assert qs.get("from") == "2026-06-01"
    assert qs.get("to") == "2026-06-30"


@respx.mock
@pytest.mark.asyncio
async def test_get_time_entry_success(connector):
    respx.get(f"{HARVEST_BASE}/time_entries/9001").mock(
        return_value=httpx.Response(200, json={"id": 9001, "hours": 1.0})
    )
    result = await connector.get_time_entry(9001)
    assert result["id"] == 9001


@respx.mock
@pytest.mark.asyncio
async def test_create_time_entry_posts_body(connector):
    route = respx.post(f"{HARVEST_BASE}/time_entries").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 9001,
                "project_id": 10,
                "task_id": 7,
                "spent_date": "2026-06-21",
                "hours": 1.25,
            },
        )
    )
    result = await connector.create_time_entry(
        project_id=10, task_id=7, spent_date="2026-06-21", hours=1.25, notes="review"
    )
    assert result["id"] == 9001
    import json as _json
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["project_id"] == 10
    assert body["task_id"] == 7
    assert body["spent_date"] == "2026-06-21"
    assert body["hours"] == 1.25
    assert body["notes"] == "review"


@respx.mock
@pytest.mark.asyncio
async def test_update_time_entry(connector):
    route = respx.patch(f"{HARVEST_BASE}/time_entries/9001").mock(
        return_value=httpx.Response(
            200, json={"id": 9001, "hours": 2.0, "notes": "updated"}
        )
    )
    result = await connector.update_time_entry(
        time_entry_id=9001, fields={"hours": 2.0, "notes": "updated"}
    )
    assert result["hours"] == 2.0
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_delete_time_entry(connector):
    route = respx.delete(f"{HARVEST_BASE}/time_entries/9001").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_time_entry(time_entry_id=9001)
    assert result == {}
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Invoices / Estimates / Expenses
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_invoices_with_state(connector):
    route = respx.get(f"{HARVEST_BASE}/invoices").mock(
        return_value=httpx.Response(
            200,
            json={"invoices": [{"id": 1, "number": "I-001", "state": "open"}], "next_page": None},
        )
    )
    result = await connector.list_invoices(state="open")
    assert result["invoices"][0]["state"] == "open"
    assert route.calls[0].request.url.params.get("state") == "open"


@respx.mock
@pytest.mark.asyncio
async def test_list_estimates_success(connector):
    respx.get(f"{HARVEST_BASE}/estimates").mock(
        return_value=httpx.Response(
            200, json={"estimates": [{"id": 1, "number": "E-001"}], "next_page": None}
        )
    )
    result = await connector.list_estimates()
    assert result["estimates"][0]["number"] == "E-001"


@respx.mock
@pytest.mark.asyncio
async def test_list_expenses_success(connector):
    respx.get(f"{HARVEST_BASE}/expenses").mock(
        return_value=httpx.Response(
            200, json={"expenses": [{"id": 22, "total_cost": 99.0}], "next_page": None}
        )
    )
    result = await connector.list_expenses(from_date="2026-01-01")
    assert result["expenses"][0]["id"] == 22


# ═══════════════════════════════════════════════════════════════════════════
# Error mapping: 404, 422
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_not_found_on_404(connector):
    respx.get(f"{HARVEST_BASE}/time_entries/12345").mock(
        return_value=httpx.Response(404, json={"message": "missing"})
    )
    with pytest.raises(HarvestNotFound):
        await connector.get_time_entry(12345)


@respx.mock
@pytest.mark.asyncio
async def test_bad_request_on_422(connector):
    respx.post(f"{HARVEST_BASE}/time_entries").mock(
        return_value=httpx.Response(422, json={"message": "validation failed"})
    )
    with pytest.raises(HarvestBadRequestError):
        await connector.create_time_entry(project_id=0, task_id=0, spent_date="bad")


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the eventual payload."""
    route = respx.get(f"{HARVEST_BASE}/users/me").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
            httpx.Response(200, json={"id": 1, "first_name": "after-retry"}),
        ]
    )
    result = await connector.get_user_me()
    assert route.call_count == 2
    assert result["first_name"] == "after-retry"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry too."""
    route = respx.get(f"{HARVEST_BASE}/users/me").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    result = await connector.get_user_me()
    assert route.call_count == 2
    assert result["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_429_exhausted_raises_rate_limit_error(connector, no_retry_sleep):
    """If 429 persists past max retries, surface HarvestRateLimitError."""
    respx.get(f"{HARVEST_BASE}/users/me").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow"}),
    )
    with pytest.raises(HarvestRateLimitError):
        await connector.get_user_me()


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert HarvestConnector.CONNECTOR_TYPE == "harvest"


def test_auth_type_class_attr():
    assert HarvestConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(HarvestConnector, "REQUIRED_CONFIG_KEYS")
    assert "access_token" in HarvestConnector.REQUIRED_CONFIG_KEYS
    assert "account_id" in HarvestConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = HarvestConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = HarvestConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer ID uses tenant_id_source_id (SOC compliance)
# ═══════════════════════════════════════════════════════════════════════════

def test_normalizer_id_uses_tenant_prefix():
    from helpers.normalizer import normalize_time_entry
    doc = normalize_time_entry(
        {"id": 9001, "spent_date": "2026-06-01", "hours": 1.0,
         "project": {"id": 10, "name": "Web"},
         "task": {"id": 7, "name": "Design"},
         "user": {"id": 1, "name": "V"}},
        connector_id="conn-x",
        tenant_id="tnt-A",
    )
    assert doc.id == "tnt-A_9001"
    assert doc.source_id == "9001"
    assert doc.tenant_id == "tnt-A"
    assert doc.source == "harvest.time_entries"
