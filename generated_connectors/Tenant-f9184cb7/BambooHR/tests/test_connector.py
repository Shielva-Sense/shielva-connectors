"""Unit tests for BambooHRConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import BambooHRConnector, _normalize_time_off
from exceptions import (
    BambooHRAuthError,
    BambooHRError,
    BambooHRNetworkError,
    BambooHRNotFoundError,
    BambooHRRateLimitError,
)
from helpers.utils import normalize_employee, with_retry
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ──────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_bamboohr_test_001"
COMPANY_DOMAIN = "acme"
API_KEY = "BAMBOOHR_API_KEY_TEST"

SAMPLE_EMPLOYEE: dict = {
    "id": "101",
    "firstName": "Jane",
    "lastName": "Smith",
    "displayName": "Jane Smith",
    "jobTitle": "Software Engineer",
    "department": "Engineering",
    "location": "San Francisco, CA",
    "workEmail": "jane.smith@acme.com",
    "mobilePhone": "+1-555-1234",
    "hireDate": "2023-03-15",
    "status": "Active",
}

SAMPLE_EMPLOYEE_2: dict = {
    "id": "102",
    "firstName": "Bob",
    "lastName": "Jones",
    "displayName": "Bob Jones",
    "jobTitle": "Product Manager",
    "department": "Product",
    "location": "New York, NY",
    "workEmail": "bob.jones@acme.com",
    "mobilePhone": "+1-555-5678",
    "hireDate": "2022-07-01",
    "status": "Active",
}

SAMPLE_DIRECTORY_RESPONSE: dict = {
    "meta": {"companyName": "Acme Corp"},
    "fields": [
        {"id": "firstName", "type": "text", "name": "First Name"},
        {"id": "lastName", "type": "text", "name": "Last Name"},
    ],
    "employees": [SAMPLE_EMPLOYEE],
}

SAMPLE_DIRECTORY_MULTI: dict = {
    "meta": {"companyName": "Acme Corp"},
    "fields": [],
    "employees": [SAMPLE_EMPLOYEE, SAMPLE_EMPLOYEE_2],
}

SAMPLE_EMPLOYEE_DETAIL: dict = {
    "id": "101",
    "firstName": "Jane",
    "lastName": "Smith",
    "jobTitle": "Software Engineer",
    "department": "Engineering",
    "workEmail": "jane.smith@acme.com",
    "hireDate": "2023-03-15",
}

SAMPLE_TIME_OFF_REQUEST: dict = {
    "id": "5001",
    "employeeId": "101",
    "name": "Jane Smith",
    "type": {"name": "Vacation"},
    "status": {"status": "approved"},
    "start": "2026-07-14",
    "end": "2026-07-18",
    "amount": {"amount": "5", "unit": "days"},
    "notes": {"employee": {"note": "Summer vacation"}},
}

SAMPLE_TIME_OFF_LIST: list = [SAMPLE_TIME_OFF_REQUEST]

SAMPLE_CUSTOM_REPORT: dict = {
    "title": "Employee Report",
    "fields": ["firstName", "lastName", "department"],
    "employees": [
        {"id": "101", "firstName": "Jane", "lastName": "Smith", "department": "Engineering"}
    ],
}

SAMPLE_COMPANY_INFO: dict = {
    "id": "1",
    "name": "Acme Corp",
    "defaultCurrency": "USD",
    "timeZone": "America/Los_Angeles",
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> BambooHRConnector:
    return BambooHRConnector(
        company_domain=COMPANY_DOMAIN,
        api_key=API_KEY,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: BambooHRConnector) -> BambooHRConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


# ── install() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success(connector: BambooHRConnector) -> None:
    with patch("connector.BambooHRHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_employee_directory = AsyncMock(return_value=SAMPLE_DIRECTORY_RESPONSE)
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Acme Corp" in result.message


@pytest.mark.asyncio
async def test_install_missing_company_domain() -> None:
    c = BambooHRConnector(api_key=API_KEY, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "company_domain" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = BambooHRConnector(company_domain=COMPANY_DOMAIN, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_missing_all_fields() -> None:
    c = BambooHRConnector(tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector: BambooHRConnector) -> None:
    with patch("connector.BambooHRHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_employee_directory = AsyncMock(
            side_effect=BambooHRAuthError("Invalid credentials", 401)
        )
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid credentials" in result.message


@pytest.mark.asyncio
async def test_install_network_error(connector: BambooHRConnector) -> None:
    with patch("connector.BambooHRHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_employee_directory = AsyncMock(
            side_effect=BambooHRNetworkError("Connection refused")
        )
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_error(connector: BambooHRConnector) -> None:
    with patch("connector.BambooHRHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_employee_directory = AsyncMock(
            side_effect=RuntimeError("unexpected")
        )
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_fallback_company_name(connector: BambooHRConnector) -> None:
    """When meta.companyName is absent, falls back to company_domain."""
    with patch("connector.BambooHRHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_employee_directory = AsyncMock(
            return_value={"employees": []}  # no meta
        )
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert COMPANY_DOMAIN in result.message


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: BambooHRConnector) -> None:
    with patch("connector.BambooHRHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_employee_directory = AsyncMock(return_value=SAMPLE_DIRECTORY_RESPONSE)
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Acme Corp" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: BambooHRConnector) -> None:
    with patch("connector.BambooHRHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_employee_directory = AsyncMock(
            side_effect=BambooHRAuthError("Forbidden", 403)
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: BambooHRConnector) -> None:
    with patch("connector.BambooHRHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_employee_directory = AsyncMock(
            side_effect=BambooHRNetworkError("Timeout")
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = BambooHRConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: BambooHRConnector) -> None:
    with patch("connector.BambooHRHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_employee_directory = AsyncMock(side_effect=RuntimeError("boom"))
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee_directory = AsyncMock(
        return_value={"meta": {"companyName": "Acme"}, "employees": []}
    )
    c._http_client.list_time_off_requests = AsyncMock(return_value=[])
    result = await c.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_single_employee(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee_directory = AsyncMock(return_value=SAMPLE_DIRECTORY_RESPONSE)
    c._http_client.list_time_off_requests = AsyncMock(return_value=[])
    result = await c.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_multiple_employees(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee_directory = AsyncMock(return_value=SAMPLE_DIRECTORY_MULTI)
    c._http_client.list_time_off_requests = AsyncMock(return_value=[])
    result = await c.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_with_time_off(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee_directory = AsyncMock(return_value=SAMPLE_DIRECTORY_RESPONSE)
    c._http_client.list_time_off_requests = AsyncMock(return_value=SAMPLE_TIME_OFF_LIST)
    result = await c.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2  # 1 employee + 1 time-off
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_api_error_returns_failed(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee_directory = AsyncMock(
        side_effect=BambooHRError("server error", 500)
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_partial_failure(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee_directory = AsyncMock(return_value=SAMPLE_DIRECTORY_MULTI)
    c._http_client.list_time_off_requests = AsyncMock(return_value=[])
    # Patch normalize_employee to fail on second employee
    from helpers.utils import normalize_employee as original_normalize
    call_count = {"n": 0}

    def flaky_normalize(*args, **kwargs):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ValueError("normalize failure")
        return original_normalize(*args, **kwargs)

    with patch("connector.normalize_employee", side_effect=flaky_normalize):
        result = await c.sync(full=True)

    assert result.status == SyncStatus.PARTIAL
    assert result.documents_found == 2
    assert result.documents_synced == 1
    assert result.documents_failed == 1


@pytest.mark.asyncio
async def test_sync_time_off_failure_is_non_fatal(connector_with_mock_client: BambooHRConnector) -> None:
    """Time-off sync failure must not fail the whole sync."""
    c = connector_with_mock_client
    c._http_client.get_employee_directory = AsyncMock(return_value=SAMPLE_DIRECTORY_RESPONSE)
    c._http_client.list_time_off_requests = AsyncMock(
        side_effect=BambooHRNetworkError("time-off API down")
    )
    result = await c.sync(full=True)
    # Employee sync should still complete
    assert result.documents_synced >= 1
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


# ── get_employee_directory() ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_employee_directory(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee_directory = AsyncMock(return_value=SAMPLE_DIRECTORY_RESPONSE)
    result = await c.get_employee_directory()
    assert "employees" in result
    assert result["employees"][0]["id"] == "101"


@pytest.mark.asyncio
async def test_get_employee_directory_empty(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee_directory = AsyncMock(
        return_value={"meta": {}, "employees": []}
    )
    result = await c.get_employee_directory()
    assert result["employees"] == []


# ── get_employee() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_employee_basic(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee = AsyncMock(return_value=SAMPLE_EMPLOYEE_DETAIL)
    result = await c.get_employee("101")
    assert result["id"] == "101"
    assert result["firstName"] == "Jane"


@pytest.mark.asyncio
async def test_get_employee_with_fields(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee = AsyncMock(
        return_value={"id": "101", "firstName": "Jane", "department": "Engineering"}
    )
    result = await c.get_employee("101", fields=["firstName", "department"])
    assert "firstName" in result
    assert "department" in result
    call_kwargs = c._http_client.get_employee.call_args
    assert call_kwargs.kwargs.get("fields") == ["firstName", "department"]


@pytest.mark.asyncio
async def test_get_employee_not_found(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee = AsyncMock(
        side_effect=BambooHRNotFoundError("employee", "9999")
    )
    with pytest.raises(BambooHRNotFoundError):
        await c.get_employee("9999")


@pytest.mark.asyncio
async def test_get_employee_passes_id(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_employee = AsyncMock(return_value=SAMPLE_EMPLOYEE_DETAIL)
    await c.get_employee(101)
    call_args = c._http_client.get_employee.call_args
    assert 101 in call_args.args


# ── list_time_off_requests() ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_time_off_requests_returns_list(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_time_off_requests = AsyncMock(return_value=SAMPLE_TIME_OFF_LIST)
    result = await c.list_time_off_requests("2026-01-01", "2026-12-31")
    assert isinstance(result, list)
    assert result[0]["id"] == "5001"


@pytest.mark.asyncio
async def test_list_time_off_requests_passes_dates(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_time_off_requests = AsyncMock(return_value=[])
    await c.list_time_off_requests("2026-06-01", "2026-06-30")
    call_args = c._http_client.list_time_off_requests.call_args
    assert "2026-06-01" in call_args.args
    assert "2026-06-30" in call_args.args


@pytest.mark.asyncio
async def test_list_time_off_empty(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_time_off_requests = AsyncMock(return_value=[])
    result = await c.list_time_off_requests("2026-01-01", "2026-12-31")
    assert result == []


# ── list_custom_reports() ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_custom_reports(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_custom_reports = AsyncMock(return_value=SAMPLE_CUSTOM_REPORT)
    result = await c.list_custom_reports(42)
    assert result["title"] == "Employee Report"
    assert len(result["employees"]) == 1


@pytest.mark.asyncio
async def test_list_custom_reports_passes_id(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_custom_reports = AsyncMock(return_value=SAMPLE_CUSTOM_REPORT)
    await c.list_custom_reports("99")
    call_args = c._http_client.list_custom_reports.call_args
    assert "99" in call_args.args


# ── get_company_info() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_company_info(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_company_info = AsyncMock(return_value=SAMPLE_COMPANY_INFO)
    result = await c.get_company_info()
    assert result["name"] == "Acme Corp"
    assert result["defaultCurrency"] == "USD"


@pytest.mark.asyncio
async def test_get_company_info_not_found(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_company_info = AsyncMock(
        side_effect=BambooHRNotFoundError("company/info", "")
    )
    with pytest.raises(BambooHRNotFoundError):
        await c.get_company_info()


# ── normalize_employee() ──────────────────────────────────────────────────────


def test_normalize_employee_basic() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    assert doc.title == "Employee: Jane Smith"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert f"{COMPANY_DOMAIN}.bamboohr.com" in doc.source_url


def test_normalize_employee_source_id_is_16_chars() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    assert len(doc.source_id) == 16


def test_normalize_employee_source_id_is_hex() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    int(doc.source_id, 16)  # raises ValueError if not hex


def test_normalize_employee_source_id_deterministic() -> None:
    doc1 = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    doc2 = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    assert doc1.source_id == doc2.source_id


def test_normalize_employee_different_ids_produce_different_source_ids() -> None:
    emp2 = {**SAMPLE_EMPLOYEE, "id": "999"}
    doc1 = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    doc2 = normalize_employee(emp2, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    assert doc1.source_id != doc2.source_id


def test_normalize_employee_source_id_uses_employee_prefix() -> None:
    import hashlib
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    expected = hashlib.sha256(b"employee:101").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_employee_metadata_fields() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    meta = doc.metadata
    assert meta["employee_id"] == "101"
    assert meta["first_name"] == "Jane"
    assert meta["last_name"] == "Smith"
    assert meta["job_title"] == "Software Engineer"
    assert meta["department"] == "Engineering"
    assert meta["location"] == "San Francisco, CA"
    assert meta["work_email"] == "jane.smith@acme.com"
    assert meta["hire_date"] == "2023-03-15"
    assert meta["status"] == "Active"


def test_normalize_employee_content_includes_name() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    assert "Jane Smith" in doc.content


def test_normalize_employee_content_includes_department() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    assert "Engineering" in doc.content


def test_normalize_employee_content_includes_email() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    assert "jane.smith@acme.com" in doc.content


def test_normalize_employee_no_display_name_fallback() -> None:
    emp = {**SAMPLE_EMPLOYEE}
    del emp["displayName"]
    doc = normalize_employee(emp, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    assert "Jane Smith" in doc.title


def test_normalize_employee_missing_all_name_fields() -> None:
    emp = {"id": "200"}
    doc = normalize_employee(emp, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    assert "200" in doc.title


def test_normalize_employee_camel_case_and_snake_case_fields() -> None:
    emp = {
        "id": "150",
        "firstName": "Alice",
        "lastName": "Wang",
        "work_email": "alice@acme.com",
        "hire_date": "2024-01-10",
    }
    doc = normalize_employee(emp, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN)
    assert doc.metadata["work_email"] == "alice@acme.com"
    assert doc.metadata["hire_date"] == "2024-01-10"


def test_normalize_time_off_basic() -> None:
    doc = _normalize_time_off(
        SAMPLE_TIME_OFF_REQUEST, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN
    )
    assert "5001" in doc.title
    assert "Jane Smith" in doc.title
    assert doc.source_id is not None
    assert len(doc.source_id) == 16


def test_normalize_time_off_source_id_is_hex() -> None:
    doc = _normalize_time_off(
        SAMPLE_TIME_OFF_REQUEST, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN
    )
    int(doc.source_id, 16)


def test_normalize_time_off_metadata() -> None:
    doc = _normalize_time_off(
        SAMPLE_TIME_OFF_REQUEST, CONNECTOR_ID, TENANT_ID, COMPANY_DOMAIN
    )
    meta = doc.metadata
    assert meta["request_id"] == "5001"
    assert meta["employee_id"] == "101"
    assert meta["employee_name"] == "Jane Smith"
    assert meta["start"] == "2026-07-14"
    assert meta["end"] == "2026-07-18"


# ── with_retry() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    mock_fn = AsyncMock(
        side_effect=[
            BambooHRNetworkError("fail"),
            BambooHRNetworkError("fail"),
            {"ok": True},
        ]
    )
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=BambooHRAuthError("invalid creds", 401))
    with pytest.raises(BambooHRAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=BambooHRNetworkError("persistent failure"))
    with pytest.raises(BambooHRNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=BambooHRRateLimitError("429", retry_after=0))
    with pytest.raises(BambooHRRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_hierarchy_auth_is_bamboohr_error() -> None:
    exc = BambooHRAuthError("bad creds", 401)
    assert isinstance(exc, BambooHRError)


def test_exception_hierarchy_rate_limit_is_bamboohr_error() -> None:
    exc = BambooHRRateLimitError("too fast")
    assert isinstance(exc, BambooHRError)
    assert exc.retry_after == 0.0


def test_exception_hierarchy_not_found_is_bamboohr_error() -> None:
    exc = BambooHRNotFoundError("employee", 42)
    assert isinstance(exc, BambooHRError)
    assert exc.status_code == 404
    assert "42" in str(exc)


def test_exception_hierarchy_network_is_bamboohr_error() -> None:
    exc = BambooHRNetworkError("timeout", 500)
    assert isinstance(exc, BambooHRError)


def test_rate_limit_stores_retry_after() -> None:
    exc = BambooHRRateLimitError("slow down", retry_after=60.0)
    assert exc.retry_after == 60.0


# ── HTTP client internals ─────────────────────────────────────────────────────


def test_base_url_construction() -> None:
    from client.http_client import _build_base_url
    url = _build_base_url("acme")
    assert url == "https://api.bamboohr.com/api/gateway.php/acme/v1"


def test_base_url_construction_different_domains() -> None:
    from client.http_client import _build_base_url
    assert _build_base_url("shielva") == "https://api.bamboohr.com/api/gateway.php/shielva/v1"
    assert _build_base_url("big-company") == "https://api.bamboohr.com/api/gateway.php/big-company/v1"


# ── Connector config loading ──────────────────────────────────────────────────


def test_connector_loads_from_config_dict() -> None:
    c = BambooHRConnector(config={
        "company_domain": "testco",
        "api_key": "secret_key",
    })
    assert c._company_domain == "testco"
    assert c._api_key == "secret_key"


def test_connector_keyword_args_override_empty_config() -> None:
    c = BambooHRConnector(company_domain="kwarg_co", api_key="kwkey")
    assert c._company_domain == "kwarg_co"
    assert c._api_key == "kwkey"


def test_connector_config_takes_precedence_over_kwargs() -> None:
    c = BambooHRConnector(
        config={"company_domain": "from_config", "api_key": "cfg_key"},
        company_domain="from_kwarg",
        api_key="kwarg_key",
    )
    assert c._company_domain == "from_config"
    assert c._api_key == "cfg_key"


def test_connector_missing_credentials_list_all_missing() -> None:
    c = BambooHRConnector()
    missing = c._missing_credentials()
    assert "company_domain" in missing
    assert "api_key" in missing


def test_connector_missing_credentials_partial() -> None:
    c = BambooHRConnector(company_domain="acme")
    missing = c._missing_credentials()
    assert "company_domain" not in missing
    assert "api_key" in missing


def test_connector_defaults_tenant_and_connector_id() -> None:
    c = BambooHRConnector()
    assert c._tenant_id == ""
    assert c.connector_id == ""


# ── Connector lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_context_manager(connector: BambooHRConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(connector_with_mock_client: BambooHRConnector) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


def test_ensure_client_creates_on_first_call(connector: BambooHRConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: BambooHRConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2


def test_connector_type_constants() -> None:
    assert BambooHRConnector.CONNECTOR_TYPE == "bamboohr"
    assert BambooHRConnector.AUTH_TYPE == "api_key"
