"""Unit tests for HiBobConnector — respx-mocked, zero real I/O."""
import base64 as _b64
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import HiBobConnector
from exceptions import (
    HiBobAuthError,
    HiBobError,
    HiBobNotFound,
    HiBobNotFoundError,
)

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_CONFIG,
    TEST_SERVICE_USER_ID,
    TEST_SERVICE_USER_TOKEN,
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
async def test_install_missing_token(connector):
    connector.config.pop("service_user_token", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_service_user_id(connector):
    connector.config.pop("service_user_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_basic_tokeninfo(connector):
    info = await connector.authorize()
    assert info.token_type == "Basic"
    assert info.access_token == TEST_SERVICE_USER_TOKEN
    assert info.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape — Basic base64(id:token)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_request_carries_http_basic_auth(connector):
    route = respx.get(f"{BASE_URL}/people/emp-1").mock(
        return_value=httpx.Response(200, json={"id": "emp-1"})
    )
    await connector.get_employee("emp-1")
    sent_auth = route.calls.last.request.headers.get("authorization", "")
    assert sent_auth.startswith("Basic ")
    decoded = _b64.b64decode(sent_auth.split(" ", 1)[1]).decode("ascii")
    assert decoded == f"{TEST_SERVICE_USER_ID}:{TEST_SERVICE_USER_TOKEN}"


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{BASE_URL}/people").mock(
        return_value=httpx.Response(200, json={"employees": []})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_degraded(connector, no_retry_sleep):
    respx.get(f"{BASE_URL}/people").mock(
        return_value=httpx.Response(401, json={"error": "Invalid credentials"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_degraded(connector, no_retry_sleep):
    respx.get(f"{BASE_URL}/people").mock(
        return_value=httpx.Response(403, json={"error": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# list_people  — GET /people
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_people_passes_limit_and_fields(connector):
    route = respx.get(f"{BASE_URL}/people").mock(
        return_value=httpx.Response(
            200,
            json={"employees": [{"id": "emp-1"}, {"id": "emp-2"}]},
        )
    )
    resp = await connector.list_people(limit=20, fields=["id", "firstName"])
    assert route.called
    qs = dict(route.calls.last.request.url.params)
    assert qs["limit"] == "20"
    assert qs["fields"] == "id,firstName"
    assert resp["employees"][0]["id"] == "emp-1"


# ═══════════════════════════════════════════════════════════════════════════
# search_people  — POST /people/search
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_search_people_posts_default_body(connector):
    route = respx.post(f"{BASE_URL}/people/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "employees": [
                    {
                        "id": "emp-1",
                        "firstName": "Ada",
                        "surname": "Lovelace",
                        "email": "ada@example.com",
                    }
                ]
            },
        )
    )
    resp = await connector.search_people()
    assert route.called
    body = _json.loads(route.calls.last.request.content.decode("utf-8"))
    assert "fields" in body
    assert body["filters"] == []
    assert resp["employees"][0]["id"] == "emp-1"


@respx.mock
@pytest.mark.asyncio
async def test_search_people_humanized(connector):
    respx.post(f"{BASE_URL}/people/search").mock(
        return_value=httpx.Response(
            200,
            json={"employees": [{"id": "emp-1", "first_name": "Ada", "surname": "Lovelace"}]},
        )
    )
    resp = await connector.search_people(
        include_humanized=True, fields_humanized=["first_name"]
    )
    assert "humanized" in resp["employees"][0]
    assert resp["employees"][0]["humanized"] == {"First Name": "Ada"}


@respx.mock
@pytest.mark.asyncio
async def test_search_people_custom_filters(connector):
    route = respx.post(f"{BASE_URL}/people/search").mock(
        return_value=httpx.Response(200, json={"employees": []})
    )
    filters = [{"fieldPath": "/work/department", "operator": "equals", "values": ["Eng"]}]
    fields = ["/root/id", "/root/email"]
    await connector.search_people(filters=filters, fields=fields)
    body = _json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body == {"fields": fields, "filters": filters}


# ═══════════════════════════════════════════════════════════════════════════
# get_employee  — GET /people/{id}
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_get_employee_success(connector):
    respx.get(f"{BASE_URL}/people/emp-42").mock(
        return_value=httpx.Response(200, json={"id": "emp-42", "firstName": "Grace"})
    )
    emp = await connector.get_employee("emp-42")
    assert emp["id"] == "emp-42"
    assert emp["firstName"] == "Grace"


@respx.mock
@pytest.mark.asyncio
async def test_get_employee_not_found_raises(connector):
    respx.get(f"{BASE_URL}/people/missing").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    with pytest.raises(HiBobNotFound):
        await connector.get_employee("missing")


# ═══════════════════════════════════════════════════════════════════════════
# get_employee_profile  — GET /profiles/{id}
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_get_employee_profile(connector):
    respx.get(f"{BASE_URL}/profiles/emp-7").mock(
        return_value=httpx.Response(200, json={"id": "emp-7", "displayName": "Linus T."})
    )
    profile = await connector.get_employee_profile("emp-7")
    assert profile["displayName"] == "Linus T."


# ═══════════════════════════════════════════════════════════════════════════
# create_employee / update_employee
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_create_employee_envelope(connector):
    route = respx.post(f"{BASE_URL}/people").mock(
        return_value=httpx.Response(201, json={"id": "emp-new"})
    )
    resp = await connector.create_employee(
        first_name="Linus",
        surname="Torvalds",
        email="linus@example.com",
        work_email="linus@work.example.com",
        start_date="2026-07-01",
        site="Helsinki",
    )
    sent = _json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent["firstName"] == "Linus"
    assert sent["surname"] == "Torvalds"
    assert sent["email"] == "linus@example.com"
    assert sent["workEmail"] == "linus@work.example.com"
    assert sent["work"]["startDate"] == "2026-07-01"
    assert sent["work"]["site"] == "Helsinki"
    assert resp["id"] == "emp-new"


@respx.mock
@pytest.mark.asyncio
async def test_update_employee(connector):
    route = respx.put(f"{BASE_URL}/people/emp-1").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    resp = await connector.update_employee("emp-1", {"work": {"title": "Architect"}})
    sent = _json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent == {"work": {"title": "Architect"}}
    assert resp == {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
# list_employments
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_employments(connector):
    respx.get(f"{BASE_URL}/people/emp-1/employment").mock(
        return_value=httpx.Response(200, json={"entries": [{"id": "empl-1"}]})
    )
    resp = await connector.list_employments("emp-1")
    assert resp["entries"][0]["id"] == "empl-1"


# ═══════════════════════════════════════════════════════════════════════════
# list_time_off_requests + create_time_off_request
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_time_off_requests_passes_query_params(connector):
    route = respx.get(f"{BASE_URL}/timeoff/requests/changes").mock(
        return_value=httpx.Response(200, json={"changes": []})
    )
    await connector.list_time_off_requests(
        from_date="2026-06-01",
        to_date="2026-06-30",
        policy_type_display_name="Vacation",
        include_pending=False,
    )
    qs = dict(route.calls.last.request.url.params)
    assert qs["from"] == "2026-06-01"
    assert qs["to"] == "2026-06-30"
    assert qs["policyTypeDisplayName"] == "Vacation"
    assert qs["includePending"] == "false"


@respx.mock
@pytest.mark.asyncio
async def test_create_time_off_request(connector):
    route = respx.post(f"{BASE_URL}/timeoff/employees/emp-1/requests").mock(
        return_value=httpx.Response(201, json={"requestId": "req-1"})
    )
    resp = await connector.create_time_off_request(
        employee_id="emp-1",
        policy_type_display_name="Vacation",
        request_range_type="days",
        start_date="2026-07-01",
        end_date="2026-07-05",
        description="summer",
    )
    sent = _json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent["policyTypeDisplayName"] == "Vacation"
    assert sent["requestRangeType"] == "days"
    assert sent["startDate"] == "2026-07-01"
    assert sent["endDate"] == "2026-07-05"
    assert sent["description"] == "summer"
    assert resp["requestId"] == "req-1"


# ═══════════════════════════════════════════════════════════════════════════
# list_payroll
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_payroll(connector):
    respx.get(f"{BASE_URL}/payroll/history/emp-1").mock(
        return_value=httpx.Response(
            200, json={"history": [{"period": "2026-05", "gross": 5000}]}
        )
    )
    resp = await connector.list_payroll("emp-1")
    assert resp["history"][0]["gross"] == 5000


# ═══════════════════════════════════════════════════════════════════════════
# list_lifecycle_changes
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_lifecycle_changes_with_window(connector):
    route = respx.get(f"{BASE_URL}/people/lifecycle/changes").mock(
        return_value=httpx.Response(200, json={"changes": []})
    )
    await connector.list_lifecycle_changes(from_date="2026-01-01", to_date="2026-06-30")
    qs = dict(route.calls.last.request.url.params)
    assert qs["from"] == "2026-01-01"
    assert qs["to"] == "2026-06-30"


@respx.mock
@pytest.mark.asyncio
async def test_list_lifecycle_changes_no_window(connector):
    route = respx.get(f"{BASE_URL}/people/lifecycle/changes").mock(
        return_value=httpx.Response(200, json={"changes": []})
    )
    await connector.list_lifecycle_changes()
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# list_departments + list_sites
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_departments(connector):
    respx.get(f"{BASE_URL}/company/named-lists/department").mock(
        return_value=httpx.Response(200, json={"items": ["Eng", "Sales"]})
    )
    resp = await connector.list_departments()
    assert resp["items"] == ["Eng", "Sales"]


@respx.mock
@pytest.mark.asyncio
async def test_list_sites(connector):
    respx.get(f"{BASE_URL}/company/named-lists/site").mock(
        return_value=httpx.Response(200, json={"items": ["NYC", "London"]})
    )
    resp = await connector.list_sites()
    assert resp["items"] == ["NYC", "London"]


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — succeed on second attempt
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/people/emp-rl").mock(
        side_effect=[
            httpx.Response(
                429, headers={"Retry-After": "0"}, json={"error": "rate limit"}
            ),
            httpx.Response(200, json={"id": "emp-rl"}),
        ]
    )
    emp = await connector.get_employee("emp-rl")
    assert route.call_count == 2
    assert emp["id"] == "emp-rl"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{BASE_URL}/people/emp-rl").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json={"id": "emp-rl"}),
        ]
    )
    emp = await connector.get_employee("emp-rl")
    assert route.call_count == 2
    assert emp["id"] == "emp-rl"


# ═══════════════════════════════════════════════════════════════════════════
# Missing credentials surface as HiBobAuthError at call time
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_call_without_credentials_raises_auth_error(mocker):
    cnx = HiBobConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "service_user_id": "",
            "service_user_token": "",
            "base_url": BASE_URL,
        },
    )
    mocker.patch("connector.logger")
    with pytest.raises(HiBobAuthError):
        await cnx.get_employee("emp-1")


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert HiBobConnector.CONNECTOR_TYPE == "hibob"


def test_auth_type_class_attr():
    assert HiBobConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(HiBobConnector, "REQUIRED_CONFIG_KEYS")
    assert "service_user_id" in HiBobConnector.REQUIRED_CONFIG_KEYS
    assert "service_user_token" in HiBobConnector.REQUIRED_CONFIG_KEYS


def test_status_map_classifies_401_403_429():
    assert HiBobConnector._STATUS_MAP[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert HiBobConnector._STATUS_MAP[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert HiBobConnector._STATUS_MAP[429] == ("DEGRADED", "CONNECTED")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation — NormalizedDocument id is tenant-scoped
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = HiBobConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = HiBobConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_normalize_employee_uses_tenant_scoped_id():
    from helpers.normalizer import normalize_employee

    raw = {"id": "emp-1", "firstName": "Ada", "surname": "Lovelace"}
    doc = normalize_employee(raw, connector_id="conn-1", tenant_id="t-A")
    assert doc.id == "t-A_emp-1"
    assert doc.source_id == "emp-1"
    assert doc.tenant_id == "t-A"
    assert doc.connector_id == "conn-1"
    assert doc.source == "hibob.people"
