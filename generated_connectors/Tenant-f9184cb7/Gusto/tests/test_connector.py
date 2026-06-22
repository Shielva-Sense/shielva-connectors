"""Unit tests for GustoConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import GustoConnector
from exceptions import (
    GustoAuthError,
    GustoNetworkError,
    GustoNotFoundError,
    GustoRateLimitError,
)
from helpers.utils import normalize_employee, normalize_payroll, with_retry
from models import AuthStatus, ConnectorDocument, ConnectorHealth, SyncStatus

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_gusto_test_001"
VALID_TOKEN = "test_access_token_gusto"
CLIENT_ID = "test-gusto-client-id"
CLIENT_SECRET = "test-gusto-client-secret"
REDIRECT_URI = "https://shielva.test/oauth/callback/gusto"

SAMPLE_ME: dict = {
    "email": "owner@acme.com",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "roles": {
        "payroll_admin": {
            "companies": [
                {"id": "company_001", "name": "Acme Corp"},
                {"id": "company_002", "name": "Beta LLC"},
            ]
        }
    },
}

SAMPLE_EMPLOYEE_1: dict = {
    "id": "emp_001",
    "first_name": "Alice",
    "last_name": "Smith",
    "email": "alice@acme.com",
    "job_title": "Engineer",
    "department": "Engineering",
    "start_date": "2022-03-01",
    "employment_status": "active",
    "terminated": False,
}

SAMPLE_EMPLOYEE_2: dict = {
    "id": "emp_002",
    "first_name": "Bob",
    "last_name": "Jones",
    "email": "bob@acme.com",
    "job_title": "Sales Rep",
    "department": "Sales",
    "start_date": "2021-07-15",
    "employment_status": "active",
    "terminated": False,
}

SAMPLE_PAYROLL_1: dict = {
    "payroll_id": "payroll_001",
    "pay_period": {"start_date": "2026-06-01", "end_date": "2026-06-15"},
    "check_date": "2026-06-20",
    "processed": True,
    "totals": {"gross_pay": "50000.00", "net_pay": "38000.00"},
    "employee_compensations": [{"employee_id": "emp_001"}, {"employee_id": "emp_002"}],
}

SAMPLE_PAYROLL_2: dict = {
    "payroll_id": "payroll_002",
    "pay_period": {"start_date": "2026-05-16", "end_date": "2026-05-31"},
    "check_date": "2026-06-05",
    "processed": True,
    "totals": {"gross_pay": "48000.00", "net_pay": "36500.00"},
    "employee_compensations": [{"employee_id": "emp_001"}],
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> GustoConnector:
    c = GustoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": VALID_TOKEN,
        },
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    return c


@pytest.fixture()
def no_token() -> GustoConnector:
    return GustoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )


@pytest.fixture()
def no_creds() -> GustoConnector:
    return GustoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_missing_both_creds(no_creds: GustoConnector) -> None:
    result = await no_creds.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id and client_secret are required" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    c = GustoConnector(config={"client_secret": CLIENT_SECRET, "access_token": VALID_TOKEN})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    c = GustoConnector(config={"client_id": CLIENT_ID, "access_token": VALID_TOKEN})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_pending_no_access_token(no_token: GustoConnector) -> None:
    result = await no_token.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert "OAuth" in result.message


@pytest.mark.asyncio
async def test_install_success_with_token() -> None:
    c = GustoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": VALID_TOKEN,
        },
    )
    with patch("connector.GustoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=SAMPLE_ME)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "owner@acme.com" in result.message


@pytest.mark.asyncio
async def test_install_invalid_token() -> None:
    c = GustoConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": "expired_token",
        }
    )
    with patch("connector.GustoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(
            side_effect=GustoAuthError("Token expired", 401)
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Token expired" in result.message


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = GustoConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": VALID_TOKEN,
        }
    )
    with patch("connector.GustoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=GustoNetworkError("Connection refused"))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ── authorize() ──────────────────────────────────────────────────────────────


def test_authorize_returns_url_with_client_id(no_token: GustoConnector) -> None:
    url = no_token.authorize()
    assert "api.gusto.com/oauth/authorize" in url
    assert CLIENT_ID in url


def test_authorize_includes_scopes(no_token: GustoConnector) -> None:
    url = no_token.authorize()
    assert "employees%3Aread" in url or "employees:read" in url


def test_authorize_includes_redirect_uri() -> None:
    c = GustoConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        }
    )
    url = c.authorize()
    assert "redirect_uri" in url
    assert "shielva.test" in url


def test_authorize_omits_redirect_when_not_set(no_token: GustoConnector) -> None:
    url = no_token.authorize()
    assert "redirect_uri" not in url


def test_authorize_includes_response_type(no_token: GustoConnector) -> None:
    url = no_token.authorize()
    assert "response_type=code" in url


# ── health_check() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_no_token(no_token: GustoConnector) -> None:
    result = await no_token.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token is required" in result.message


@pytest.mark.asyncio
async def test_health_check_healthy(authed: GustoConnector) -> None:
    with patch("connector.GustoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value=SAMPLE_ME)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "owner@acme.com" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: GustoConnector) -> None:
    with patch("connector.GustoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=GustoAuthError("Unauthorized", 401))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: GustoConnector) -> None:
    with patch("connector.GustoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=GustoNetworkError("Timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(authed: GustoConnector) -> None:
    with patch("connector.GustoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_no_email_in_response(authed: GustoConnector) -> None:
    with patch("connector.GustoHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_me = AsyncMock(return_value={"first_name": "Ada"})
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert "Gusto API is reachable" in result.message


# ── sync() ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_no_token() -> None:
    c = GustoConnector(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_sync_empty_companies(authed: GustoConnector) -> None:
    authed.http_client.list_companies = AsyncMock(return_value=[])
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_one_company_employees_and_payrolls(authed: GustoConnector) -> None:
    authed.http_client.list_companies = AsyncMock(
        return_value=[{"id": "company_001", "name": "Acme Corp"}]
    )
    authed.http_client.list_employees = AsyncMock(
        return_value=[SAMPLE_EMPLOYEE_1, SAMPLE_EMPLOYEE_2]
    )
    authed.http_client.list_payrolls = AsyncMock(
        return_value=[SAMPLE_PAYROLL_1, SAMPLE_PAYROLL_2]
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    # 2 employees + 2 payrolls = 4
    assert result.documents_found == 4
    assert result.documents_synced == 4
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(authed: GustoConnector) -> None:
    authed.http_client.list_companies = AsyncMock(
        return_value=[{"id": "company_001", "name": "Acme Corp"}]
    )
    authed.http_client.list_employees = AsyncMock(return_value=[SAMPLE_EMPLOYEE_1])
    authed.http_client.list_payrolls = AsyncMock(return_value=[SAMPLE_PAYROLL_1])
    ingest_calls: list = []

    async def mock_ingest(doc: ConnectorDocument, kb_id: str) -> None:
        ingest_calls.append((doc.source_id, kb_id))

    authed._ingest_document = mock_ingest  # type: ignore[method-assign]
    result = await authed.sync(full=True, kb_id="kb_gusto_123")
    assert result.documents_synced == 2
    assert all(kb_id == "kb_gusto_123" for _, kb_id in ingest_calls)


@pytest.mark.asyncio
async def test_sync_company_list_fails(authed: GustoConnector) -> None:
    authed.http_client.list_companies = AsyncMock(
        side_effect=GustoNetworkError("Network failure")
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "Failed to list companies" in result.message


@pytest.mark.asyncio
async def test_sync_two_companies(authed: GustoConnector) -> None:
    authed.http_client.list_companies = AsyncMock(
        return_value=[
            {"id": "company_001", "name": "Acme Corp"},
            {"id": "company_002", "name": "Beta LLC"},
        ]
    )
    authed.http_client.list_employees = AsyncMock(return_value=[SAMPLE_EMPLOYEE_1])
    authed.http_client.list_payrolls = AsyncMock(return_value=[SAMPLE_PAYROLL_1])
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    # 2 companies × (1 employee + 1 payroll) = 4
    assert result.documents_found == 4
    assert result.documents_synced == 4


@pytest.mark.asyncio
async def test_sync_partial_on_employee_failure(authed: GustoConnector) -> None:
    authed.http_client.list_companies = AsyncMock(
        return_value=[{"id": "company_001", "name": "Acme Corp"}]
    )
    authed.http_client.list_employees = AsyncMock(
        side_effect=GustoNetworkError("Employee list failed")
    )
    authed.http_client.list_payrolls = AsyncMock(return_value=[SAMPLE_PAYROLL_1])
    result = await authed.sync(full=True)
    # employees failed (counts as 1 failed block), payrolls succeeded
    assert result.documents_synced == 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_paginated_employees(authed: GustoConnector) -> None:
    """When a company has more than page_size employees, pagination continues."""
    # First page full (100 items), second page partial (1 item) → stops
    page1 = [{"id": f"emp_{i}", "first_name": f"User{i}", "last_name": "Test"} for i in range(100)]
    page2 = [SAMPLE_EMPLOYEE_1]
    authed.http_client.list_companies = AsyncMock(
        return_value=[{"id": "company_001", "name": "Acme Corp"}]
    )
    authed.http_client.list_employees = AsyncMock(side_effect=[page1, page2])
    authed.http_client.list_payrolls = AsyncMock(return_value=[])
    result = await authed.sync(full=True)
    assert result.documents_found == 101
    assert authed.http_client.list_employees.call_count == 2


@pytest.mark.asyncio
async def test_sync_empty_employees_and_payrolls(authed: GustoConnector) -> None:
    authed.http_client.list_companies = AsyncMock(
        return_value=[{"id": "company_001", "name": "Acme Corp"}]
    )
    authed.http_client.list_employees = AsyncMock(return_value=[])
    authed.http_client.list_payrolls = AsyncMock(return_value=[])
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0


# ── get_current_user() ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_current_user_success(authed: GustoConnector) -> None:
    authed.http_client.get_me = AsyncMock(return_value=SAMPLE_ME)
    result = await authed.get_current_user()
    assert result["email"] == "owner@acme.com"


@pytest.mark.asyncio
async def test_get_current_user_auth_error(authed: GustoConnector) -> None:
    authed.http_client.get_me = AsyncMock(side_effect=GustoAuthError("Forbidden", 403))
    with pytest.raises(GustoAuthError):
        await authed.get_current_user()


# ── list_companies() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_companies_success(authed: GustoConnector) -> None:
    authed.http_client.list_companies = AsyncMock(
        return_value=[
            {"id": "company_001", "name": "Acme Corp"},
            {"id": "company_002", "name": "Beta LLC"},
        ]
    )
    companies = await authed.list_companies()
    assert len(companies) == 2
    assert companies[0]["name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_list_companies_empty(authed: GustoConnector) -> None:
    authed.http_client.list_companies = AsyncMock(return_value=[])
    companies = await authed.list_companies()
    assert companies == []


@pytest.mark.asyncio
async def test_list_companies_auth_error(authed: GustoConnector) -> None:
    authed.http_client.list_companies = AsyncMock(
        side_effect=GustoAuthError("Unauthorized", 401)
    )
    with pytest.raises(GustoAuthError):
        await authed.list_companies()


# ── list_employees() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_employees_success(authed: GustoConnector) -> None:
    authed.http_client.list_employees = AsyncMock(
        return_value=[SAMPLE_EMPLOYEE_1, SAMPLE_EMPLOYEE_2]
    )
    employees = await authed.list_employees("company_001")
    assert len(employees) == 2
    assert employees[0]["email"] == "alice@acme.com"


@pytest.mark.asyncio
async def test_list_employees_page_param(authed: GustoConnector) -> None:
    authed.http_client.list_employees = AsyncMock(return_value=[SAMPLE_EMPLOYEE_2])
    await authed.list_employees("company_001", page=2)
    call_kwargs = authed.http_client.list_employees.call_args
    assert call_kwargs.kwargs.get("page") == 2 or 2 in call_kwargs.args


@pytest.mark.asyncio
async def test_list_employees_empty(authed: GustoConnector) -> None:
    authed.http_client.list_employees = AsyncMock(return_value=[])
    employees = await authed.list_employees("company_001")
    assert employees == []


# ── get_employee() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_employee_success(authed: GustoConnector) -> None:
    authed.http_client.get_employee = AsyncMock(return_value=SAMPLE_EMPLOYEE_1)
    emp = await authed.get_employee("company_001", "emp_001")
    assert emp["id"] == "emp_001"
    assert emp["first_name"] == "Alice"


@pytest.mark.asyncio
async def test_get_employee_not_found(authed: GustoConnector) -> None:
    authed.http_client.get_employee = AsyncMock(
        side_effect=GustoNotFoundError("employee", "bad_id")
    )
    with pytest.raises(GustoNotFoundError):
        await authed.get_employee("company_001", "bad_id")


@pytest.mark.asyncio
async def test_get_employee_auth_error(authed: GustoConnector) -> None:
    authed.http_client.get_employee = AsyncMock(
        side_effect=GustoAuthError("Forbidden", 403)
    )
    with pytest.raises(GustoAuthError):
        await authed.get_employee("company_001", "emp_001")


# ── list_payrolls() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_payrolls_success(authed: GustoConnector) -> None:
    authed.http_client.list_payrolls = AsyncMock(
        return_value=[SAMPLE_PAYROLL_1, SAMPLE_PAYROLL_2]
    )
    payrolls = await authed.list_payrolls("company_001")
    assert len(payrolls) == 2
    assert payrolls[0]["payroll_id"] == "payroll_001"


@pytest.mark.asyncio
async def test_list_payrolls_empty(authed: GustoConnector) -> None:
    authed.http_client.list_payrolls = AsyncMock(return_value=[])
    payrolls = await authed.list_payrolls("company_001")
    assert payrolls == []


@pytest.mark.asyncio
async def test_list_payrolls_unprocessed(authed: GustoConnector) -> None:
    authed.http_client.list_payrolls = AsyncMock(return_value=[])
    await authed.list_payrolls("company_001", processed=False)
    call_kwargs = authed.http_client.list_payrolls.call_args
    assert call_kwargs.kwargs.get("processed") is False or False in call_kwargs.args


# ── list_departments() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_departments_success(authed: GustoConnector) -> None:
    sample_departments = [
        {"uuid": "dept_001", "title": "Engineering"},
        {"uuid": "dept_002", "title": "Sales"},
    ]
    authed.http_client.get_departments = AsyncMock(return_value=sample_departments)
    depts = await authed.list_departments("company_001")
    assert len(depts) == 2
    assert depts[0]["title"] == "Engineering"


@pytest.mark.asyncio
async def test_list_departments_empty(authed: GustoConnector) -> None:
    authed.http_client.get_departments = AsyncMock(return_value=[])
    depts = await authed.list_departments("company_001")
    assert depts == []


@pytest.mark.asyncio
async def test_list_departments_auth_error(authed: GustoConnector) -> None:
    authed.http_client.get_departments = AsyncMock(
        side_effect=GustoAuthError("Unauthorized", 401)
    )
    with pytest.raises(GustoAuthError):
        await authed.list_departments("company_001")


# ── normalize_employee() ──────────────────────────────────────────────────────


def test_normalize_employee_basic() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE_1, "company_001", CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Employee: Alice Smith"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "Alice Smith" in doc.content
    assert "alice@acme.com" in doc.content
    assert "Engineering" in doc.content


def test_normalize_employee_stable_id() -> None:
    doc1 = normalize_employee(SAMPLE_EMPLOYEE_1, "company_001", CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_employee(SAMPLE_EMPLOYEE_1, "company_001", CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_employee_id_is_16_chars() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE_1, "company_001", CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_employee_different_ids_for_different_employees() -> None:
    doc1 = normalize_employee(SAMPLE_EMPLOYEE_1, "company_001", CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_employee(SAMPLE_EMPLOYEE_2, "company_001", CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_employee_source_url_contains_ids() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE_1, "company_001", CONNECTOR_ID, TENANT_ID)
    assert "company_001" in doc.source_url
    assert "emp_001" in doc.source_url


def test_normalize_employee_metadata_structure() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE_1, "company_001", CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["employee_id"] == "emp_001"
    assert meta["company_id"] == "company_001"
    assert meta["email"] == "alice@acme.com"
    assert meta["job_title"] == "Engineer"
    assert meta["department"] == "Engineering"
    assert meta["terminated"] is False


def test_normalize_employee_terminated_flag() -> None:
    terminated_emp = {**SAMPLE_EMPLOYEE_1, "terminated": True, "employment_status": "terminated"}
    doc = normalize_employee(terminated_emp, "company_001", CONNECTOR_ID, TENANT_ID)
    assert "Terminated: True" in doc.content
    assert doc.metadata["terminated"] is True


def test_normalize_employee_missing_optional_fields() -> None:
    minimal = {"id": "emp_min", "first_name": "Min", "last_name": "User"}
    doc = normalize_employee(minimal, "company_001", CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Employee: Min User"
    assert len(doc.source_id) == 16


def test_normalize_employee_uuid_fallback() -> None:
    emp_with_uuid = {**SAMPLE_EMPLOYEE_1, "uuid": "uuid-emp-001"}
    del emp_with_uuid["id"]  # type: ignore[misc]
    doc = normalize_employee(emp_with_uuid, "company_001", CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["employee_id"] == "uuid-emp-001"


# ── normalize_payroll() ───────────────────────────────────────────────────────


def test_normalize_payroll_basic() -> None:
    doc = normalize_payroll(SAMPLE_PAYROLL_1, "company_001", CONNECTOR_ID, TENANT_ID)
    assert "2026-06-01" in doc.title
    assert "2026-06-15" in doc.title
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "50000.00" in doc.content
    assert "38000.00" in doc.content


def test_normalize_payroll_stable_id() -> None:
    doc1 = normalize_payroll(SAMPLE_PAYROLL_1, "company_001", CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_payroll(SAMPLE_PAYROLL_1, "company_001", CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_payroll_id_is_16_chars() -> None:
    doc = normalize_payroll(SAMPLE_PAYROLL_1, "company_001", CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_payroll_different_ids_for_different_payrolls() -> None:
    doc1 = normalize_payroll(SAMPLE_PAYROLL_1, "company_001", CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_payroll(SAMPLE_PAYROLL_2, "company_001", CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_payroll_metadata_structure() -> None:
    doc = normalize_payroll(SAMPLE_PAYROLL_1, "company_001", CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["payroll_id"] == "payroll_001"
    assert meta["company_id"] == "company_001"
    assert meta["period_start"] == "2026-06-01"
    assert meta["period_end"] == "2026-06-15"
    assert meta["check_date"] == "2026-06-20"
    assert meta["processed"] is True
    assert meta["gross_pay"] == "50000.00"
    assert meta["net_pay"] == "38000.00"
    assert meta["employee_count"] == 2


def test_normalize_payroll_source_url_contains_company_id() -> None:
    doc = normalize_payroll(SAMPLE_PAYROLL_1, "company_001", CONNECTOR_ID, TENANT_ID)
    assert "company_001" in doc.source_url


def test_normalize_payroll_missing_pay_period() -> None:
    minimal = {"payroll_id": "pay_min", "processed": True, "totals": {}}
    doc = normalize_payroll(minimal, "company_001", CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Payroll: pay_min"
    assert len(doc.source_id) == 16


def test_normalize_payroll_empty_totals() -> None:
    payroll = {**SAMPLE_PAYROLL_1, "totals": {}}
    doc = normalize_payroll(payroll, "company_001", CONNECTOR_ID, TENANT_ID)
    assert "Gross Pay: 0.00" in doc.content
    assert "Net Pay: 0.00" in doc.content


# ── with_retry() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await with_retry(fn, max_attempts=3)
    assert result == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_transient_error() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise GustoNetworkError("transient")
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise GustoAuthError("Unauthorized", 401)

    with pytest.raises(GustoAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_exhausted_raises_last_error() -> None:
    async def fn() -> None:
        raise GustoNetworkError("always fails")

    with pytest.raises(GustoNetworkError, match="always fails"):
        await with_retry(fn, max_attempts=2, base_delay=0.0)


@pytest.mark.asyncio
async def test_with_retry_rate_limit_respects_retry_after() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GustoRateLimitError("Rate limited", retry_after=0.0)
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls == 2


# ── Exception hierarchy ──────────────────────────────────────────────────────


def test_exception_hierarchy() -> None:
    from exceptions import GustoError
    assert issubclass(GustoAuthError, GustoError)
    assert issubclass(GustoNetworkError, GustoError)
    assert issubclass(GustoRateLimitError, GustoError)
    assert issubclass(GustoNotFoundError, GustoError)


def test_auth_error_attributes() -> None:
    exc = GustoAuthError("Unauthorized", 401, "invalid_token")
    assert exc.status_code == 401
    assert exc.code == "invalid_token"
    assert str(exc) == "Unauthorized"


def test_rate_limit_error_retry_after() -> None:
    exc = GustoRateLimitError("Too many requests", retry_after=30.0)
    assert exc.retry_after == 30.0
    assert exc.status_code == 429
    assert exc.code == "rate_limit"


def test_not_found_error_message() -> None:
    exc = GustoNotFoundError("employee", "emp_bad_id")
    assert "emp_bad_id" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "not_found"


def test_network_error_attributes() -> None:
    exc = GustoNetworkError("Connection reset", status_code=503)
    assert exc.status_code == 503
    assert "Connection reset" in str(exc)


# ── Connector model tests ────────────────────────────────────────────────────


def test_connector_has_correct_type() -> None:
    c = GustoConnector()
    assert c.CONNECTOR_TYPE == "gusto"
    assert c.AUTH_TYPE == "oauth2"


def test_connector_config_parsing() -> None:
    c = GustoConnector(
        tenant_id="t1",
        connector_id="c1",
        config={
            "client_id": "cid",
            "client_secret": "csec",
            "redirect_uri": "https://example.com/callback",
            "access_token": "tok",
        },
    )
    assert c._client_id == "cid"
    assert c._client_secret == "csec"
    assert c._redirect_uri == "https://example.com/callback"
    assert c._access_token == "tok"
    assert c.tenant_id == "t1"
    assert c.connector_id == "c1"


def test_connector_empty_config_defaults() -> None:
    c = GustoConnector()
    assert c._client_id == ""
    assert c._client_secret == ""
    assert c._access_token == ""
    assert c.http_client is None


@pytest.mark.asyncio
async def test_connector_context_manager() -> None:
    c = GustoConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": VALID_TOKEN,
        }
    )
    async with c as conn:
        assert conn is c
    assert c.http_client is None


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    c = GustoConnector()
    await c.aclose()  # Should not raise even with no client
    await c.aclose()  # Second close should also be safe


# ── ConnectorDocument model ──────────────────────────────────────────────────


def test_connector_document_defaults() -> None:
    doc = ConnectorDocument(
        source_id="sid",
        title="My Title",
        content="Some content",
        connector_id="conn1",
        tenant_id="ten1",
    )
    assert doc.source_url == ""
    assert doc.metadata == {}


def test_connector_document_metadata_isolation() -> None:
    """Each document must have its own metadata dict, not a shared default."""
    doc1 = ConnectorDocument("id1", "T1", "C1", "conn1", "ten1")
    doc2 = ConnectorDocument("id2", "T2", "C2", "conn1", "ten1")
    doc1.metadata["key"] = "val"
    assert "key" not in doc2.metadata
