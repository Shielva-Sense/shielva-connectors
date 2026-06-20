"""Unit tests for RipplingConnector — all HTTP calls are mocked via AsyncMock.

Coverage:
  - Exceptions (5 tests)
  - Models (8 tests)
  - _make_id (4 tests)
  - normalize_employee (5 tests)
  - normalize_department (3 tests)
  - with_retry (4 tests)
  - RipplingHTTPClient init + headers + methods (10 tests)
  - Response as list vs dict (3 tests)
  - _raise_for_status (5 tests)
  - RipplingConnector.install (3 tests)
  - RipplingConnector.health_check (3 tests)
  - RipplingConnector.sync (4 tests)
  - RipplingConnector.list_employees (2 tests)
  - RipplingConnector.list_departments (2 tests)
  - RipplingConnector.list_teams (2 tests)
  Total: 63 tests
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.http_client import RipplingHTTPClient
from connector import RipplingConnector
from exceptions import (
    RipplingAuthError,
    RipplingError,
    RipplingNetworkError,
    RipplingNotFoundError,
    RipplingRateLimitError,
)
from helpers.utils import (
    _make_id,
    normalize_department,
    normalize_employee,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test fixtures ──────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_rippling_test_001"
API_KEY = "rpl_test_abc123xyz"

SAMPLE_EMPLOYEE_CAMEL: dict = {
    "id": "emp_001",
    "firstName": "Alice",
    "lastName": "Chen",
    "workEmail": "alice.chen@acme.com",
    "jobTitle": "Software Engineer",
    "department": "Engineering",
    "startDate": "2023-01-15",
    "employmentType": "FULL_TIME",
    "status": "ACTIVE",
    "manager": "mgr_002",
}

SAMPLE_EMPLOYEE_SNAKE: dict = {
    "id": "emp_002",
    "first_name": "Bob",
    "last_name": "Smith",
    "work_email": "bob.smith@acme.com",
    "job_title": "Product Manager",
    "department": "Product",
    "start_date": "2022-06-01",
    "employment_type": "FULL_TIME",
    "status": "ACTIVE",
    "manager": None,
}

SAMPLE_DEPARTMENT: dict = {
    "id": "dept_001",
    "name": "Engineering",
    "description": "Core engineering department",
    "headCount": 42,
}

SAMPLE_TEAM: dict = {
    "id": "team_001",
    "name": "Platform Team",
    "description": "Platform engineering team",
}

SAMPLE_COMPANY: dict = {
    "id": "co_001",
    "name": "Acme Corp",
}

EMPLOYEES_PAGE_1: dict = {
    "data": [SAMPLE_EMPLOYEE_CAMEL],
    "next_cursor": "100",
    "total": 2,
}

EMPLOYEES_PAGE_2: dict = {
    "data": [SAMPLE_EMPLOYEE_SNAKE],
    "total": 2,
}

DEPARTMENTS_RESPONSE: dict = {
    "data": [SAMPLE_DEPARTMENT],
    "total": 1,
}

TEAMS_RESPONSE: dict = {
    "data": [SAMPLE_TEAM],
    "total": 1,
}


# ── 1. Exceptions (5 tests) ───────────────────────────────────────────────────


def test_rippling_error_base():
    exc = RipplingError("something failed", status_code=500, code="server_error")
    assert exc.message == "something failed"
    assert exc.status_code == 500
    assert exc.code == "server_error"
    assert str(exc) == "something failed"


def test_rippling_auth_error_is_rippling_error():
    exc = RipplingAuthError("unauthorized", status_code=401, code="auth_error")
    assert isinstance(exc, RipplingError)
    assert exc.status_code == 401


def test_rippling_not_found_error():
    exc = RipplingNotFoundError("employee", "emp_999")
    assert isinstance(exc, RipplingError)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"
    assert "emp_999" in str(exc)


def test_rippling_rate_limit_error():
    exc = RipplingRateLimitError("too many requests", retry_after=30.0)
    assert isinstance(exc, RipplingError)
    assert exc.status_code == 429
    assert exc.retry_after == 30.0
    assert exc.code == "rate_limit"


def test_rippling_network_error():
    exc = RipplingNetworkError("connection refused")
    assert isinstance(exc, RipplingError)
    assert "connection refused" in exc.message


# ── 2. Models (8 tests) ───────────────────────────────────────────────────────


def test_install_result_healthy():
    from models import InstallResult
    result = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="conn_001",
        message="OK",
    )
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == "conn_001"


def test_install_result_missing_credentials():
    from models import InstallResult
    result = InstallResult(
        health=ConnectorHealth.OFFLINE,
        auth_status=AuthStatus.MISSING_CREDENTIALS,
        message="Missing api_key",
    )
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


def test_health_check_result_healthy():
    from models import HealthCheckResult
    result = HealthCheckResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        message="API OK",
    )
    assert result.health == ConnectorHealth.HEALTHY
    assert result.message == "API OK"


def test_health_check_result_degraded():
    from models import HealthCheckResult
    result = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.INVALID_CREDENTIALS,
        message="Auth failed",
    )
    assert result.health == ConnectorHealth.DEGRADED


def test_sync_result_completed():
    from models import SyncResult
    result = SyncResult(
        status=SyncStatus.COMPLETED,
        documents_found=10,
        documents_synced=10,
        documents_failed=0,
    )
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 10


def test_sync_result_partial():
    from models import SyncResult
    result = SyncResult(
        status=SyncStatus.PARTIAL,
        documents_found=5,
        documents_synced=4,
        documents_failed=1,
    )
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed == 1


def test_sync_result_failed():
    from models import SyncResult
    result = SyncResult(status=SyncStatus.FAILED, message="Network error")
    assert result.status == SyncStatus.FAILED
    assert result.message == "Network error"


def test_connector_document_defaults():
    from models import ConnectorDocument
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test Employee",
        content="Some content",
        connector_id="conn_001",
        tenant_id="Tenant-abc",
    )
    assert doc.source_url == ""
    assert doc.metadata == {}


# ── 3. _make_id (4 tests) ─────────────────────────────────────────────────────


def test_make_id_returns_16_chars():
    result = _make_id("employee", "emp_001")
    assert len(result) == 16


def test_make_id_is_deterministic():
    a = _make_id("employee", "emp_001")
    b = _make_id("employee", "emp_001")
    assert a == b


def test_make_id_differs_by_prefix():
    emp = _make_id("employee", "001")
    dept = _make_id("department", "001")
    assert emp != dept


def test_make_id_differs_by_entity_id():
    id1 = _make_id("employee", "emp_001")
    id2 = _make_id("employee", "emp_002")
    assert id1 != id2


# ── 4. normalize_employee (5 tests) ──────────────────────────────────────────


def test_normalize_employee_camelcase_fields():
    doc = normalize_employee(SAMPLE_EMPLOYEE_CAMEL)
    assert doc.title == "Alice Chen"
    assert doc.metadata["email"] == "alice.chen@acme.com"
    assert doc.metadata["job_title"] == "Software Engineer"
    assert doc.metadata["department"] == "Engineering"
    assert doc.metadata["status"] == "ACTIVE"
    assert doc.metadata["source"] == "rippling"
    assert doc.metadata["type"] == "employee"


def test_normalize_employee_snake_case_fields():
    doc = normalize_employee(SAMPLE_EMPLOYEE_SNAKE)
    assert doc.title == "Bob Smith"
    assert doc.metadata["email"] == "bob.smith@acme.com"
    assert doc.metadata["job_title"] == "Product Manager"
    assert doc.metadata["employment_type"] == "FULL_TIME"


def test_normalize_employee_stable_id():
    doc1 = normalize_employee(SAMPLE_EMPLOYEE_CAMEL)
    doc2 = normalize_employee(SAMPLE_EMPLOYEE_CAMEL)
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16


def test_normalize_employee_missing_fields():
    doc = normalize_employee({"id": "emp_empty"})
    assert doc.source_id == _make_id("employee", "emp_empty")
    assert doc.title == "emp_empty"  # fallback to id when no name
    assert doc.metadata["status"] == "ACTIVE"  # default
    assert doc.metadata["email"] is None


def test_normalize_employee_content_includes_title():
    doc = normalize_employee(SAMPLE_EMPLOYEE_CAMEL)
    assert "Alice Chen" in doc.content
    assert "Software Engineer" in doc.content


# ── 5. normalize_department (3 tests) ────────────────────────────────────────


def test_normalize_department_all_fields():
    doc = normalize_department(SAMPLE_DEPARTMENT)
    assert doc.title == "Engineering"
    assert doc.metadata["name"] == "Engineering"
    assert doc.metadata["head_count"] == 42
    assert doc.metadata["source"] == "rippling"
    assert doc.metadata["type"] == "department"


def test_normalize_department_stable_id():
    doc1 = normalize_department(SAMPLE_DEPARTMENT)
    doc2 = normalize_department(SAMPLE_DEPARTMENT)
    assert doc1.source_id == doc2.source_id


def test_normalize_department_content():
    doc = normalize_department(SAMPLE_DEPARTMENT)
    assert "Engineering" in doc.content
    assert "42" in doc.content


# ── 6. with_retry (4 tests) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt():
    call_count = 0

    async def always_succeeds():
        nonlocal call_count
        call_count += 1
        return {"ok": True}

    result = await with_retry(always_succeeds, max_attempts=3)
    assert result == {"ok": True}
    assert call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_transient_error():
    call_count = 0

    async def fails_once():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RipplingNetworkError("transient")
        return {"ok": True}

    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        result = await with_retry(fails_once, max_attempts=3, base_delay=0.0)
    assert result == {"ok": True}
    assert call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts():
    async def always_fails():
        raise RipplingNetworkError("persistent failure")

    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RipplingNetworkError, match="persistent failure"):
            await with_retry(always_fails, max_attempts=3, base_delay=0.0)


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error():
    call_count = 0

    async def auth_fail():
        nonlocal call_count
        call_count += 1
        raise RipplingAuthError("invalid key")

    with pytest.raises(RipplingAuthError):
        await with_retry(auth_fail, max_attempts=3)
    assert call_count == 1  # must not retry


# ── 7. RipplingHTTPClient (10 tests) ─────────────────────────────────────────


def test_http_client_init_with_config():
    client = RipplingHTTPClient(config={"api_key": "test_key"})
    assert client._api_key() == "test_key"


def test_http_client_bearer_header():
    client = RipplingHTTPClient(config={"api_key": "my_token"})
    headers = client._headers()
    assert headers["Authorization"] == "Bearer my_token"
    assert headers["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_http_client_get_company():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    mock_response = {"data": [SAMPLE_COMPANY]}
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = mock_response
        result = await client.get_company()
    mock_req.assert_called_once_with("GET", "/companies")
    assert result == SAMPLE_COMPANY


@pytest.mark.asyncio
async def test_http_client_list_employees_no_cursor():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = EMPLOYEES_PAGE_1
        result = await client.list_employees()
    mock_req.assert_called_once_with("GET", "/employees", params={"limit": 100, "offset": 0})
    assert result == EMPLOYEES_PAGE_1


@pytest.mark.asyncio
async def test_http_client_list_employees_with_cursor():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = EMPLOYEES_PAGE_2
        result = await client.list_employees(cursor="100", limit=50)
    mock_req.assert_called_once_with("GET", "/employees", params={"limit": 50, "offset": 100})
    assert result == EMPLOYEES_PAGE_2


@pytest.mark.asyncio
async def test_http_client_list_departments():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = DEPARTMENTS_RESPONSE
        result = await client.list_departments()
    mock_req.assert_called_once_with("GET", "/departments")
    assert result == DEPARTMENTS_RESPONSE


@pytest.mark.asyncio
async def test_http_client_list_teams():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = TEAMS_RESPONSE
        result = await client.list_teams()
    mock_req.assert_called_once_with("GET", "/teams")


@pytest.mark.asyncio
async def test_http_client_list_roles():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"data": [{"id": "role_001", "name": "Admin"}]}
        result = await client.list_roles()
    mock_req.assert_called_once_with("GET", "/roles")


@pytest.mark.asyncio
async def test_http_client_list_leaves_no_cursor():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"data": [], "total": 0}
        result = await client.list_leaves()
    mock_req.assert_called_once_with("GET", "/leaves", params={"limit": 100, "offset": 0})


@pytest.mark.asyncio
async def test_http_client_list_leaves_with_cursor():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"data": []}
        result = await client.list_leaves(cursor="200", limit=50)
    mock_req.assert_called_once_with("GET", "/leaves", params={"limit": 50, "offset": 200})


# ── 8. Response as list vs dict (3 tests) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_raise_for_status_list_response_wrapped():
    """If the API returns a bare list, _raise_for_status wraps it in dict."""
    import aiohttp

    client = RipplingHTTPClient(config={"api_key": API_KEY})

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=[SAMPLE_EMPLOYEE_CAMEL])

    result = await client._raise_for_status(mock_response)
    assert "data" in result
    assert isinstance(result["data"], list)
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_raise_for_status_dict_with_data_key():
    """If the API returns {data: [...]}, _raise_for_status passes it through."""
    client = RipplingHTTPClient(config={"api_key": API_KEY})

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"data": [SAMPLE_EMPLOYEE_CAMEL], "total": 1})

    result = await client._raise_for_status(mock_response)
    assert result["data"] == [SAMPLE_EMPLOYEE_CAMEL]
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_raise_for_status_dict_without_data_key():
    """A plain dict (e.g. single company record) is returned as-is."""
    client = RipplingHTTPClient(config={"api_key": API_KEY})

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=SAMPLE_COMPANY)

    result = await client._raise_for_status(mock_response)
    assert result == SAMPLE_COMPANY


# ── 9. _raise_for_status error codes (5 tests) ───────────────────────────────


@pytest.mark.asyncio
async def test_raise_for_status_401_raises_auth_error():
    client = RipplingHTTPClient(config={"api_key": "bad"})
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.json = AsyncMock(return_value={"message": "Unauthorized"})

    with pytest.raises(RipplingAuthError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_raise_for_status_403_raises_auth_error():
    client = RipplingHTTPClient(config={"api_key": "bad"})
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.json = AsyncMock(return_value={"message": "Forbidden"})

    with pytest.raises(RipplingAuthError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_raise_for_status_404_raises_not_found():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.json = AsyncMock(return_value={"message": "Not Found"})

    with pytest.raises(RipplingNotFoundError):
        await client._raise_for_status(mock_response)


@pytest.mark.asyncio
async def test_raise_for_status_429_raises_rate_limit():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "60"}
    mock_response.json = AsyncMock(return_value={"message": "Rate limited"})

    with pytest.raises(RipplingRateLimitError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.retry_after == 60.0


@pytest.mark.asyncio
async def test_raise_for_status_500_raises_network_error():
    client = RipplingHTTPClient(config={"api_key": API_KEY})
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.json = AsyncMock(return_value={"message": "Internal error"})

    with pytest.raises(RipplingNetworkError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.status_code == 500


# ── 10. RipplingConnector.install (3 tests) ───────────────────────────────────


@pytest.mark.asyncio
async def test_install_success():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )
    with patch.object(
        RipplingHTTPClient, "get_company", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = SAMPLE_COMPANY
        result = await connector.install()

    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Rippling" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_auth_error():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": "bad_key"},
    )
    with patch.object(
        RipplingHTTPClient, "get_company", new_callable=AsyncMock
    ) as mock_get:
        mock_get.side_effect = RipplingAuthError("unauthorized")
        result = await connector.install()

    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ── 11. RipplingConnector.health_check (3 tests) ──────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )
    with patch.object(
        RipplingHTTPClient, "get_company", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = SAMPLE_COMPANY
        result = await connector.health_check()

    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Acme Corp" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": "bad_key"},
    )
    with patch.object(
        RipplingHTTPClient, "get_company", new_callable=AsyncMock
    ) as mock_get:
        mock_get.side_effect = RipplingAuthError("invalid credentials")
        result = await connector.health_check()

    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_missing_api_key():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── 12. RipplingConnector.sync (4 tests) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_success_employees_and_departments():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )

    emp_page1 = {"data": [SAMPLE_EMPLOYEE_CAMEL], "next_cursor": None, "total": 1}
    dept_resp = {"data": [SAMPLE_DEPARTMENT], "total": 1}

    async def mock_list_employees(cursor=None, limit=100):
        return emp_page1

    async def mock_list_departments():
        return dept_resp

    with patch.object(connector, "_make_client") as mock_make:
        mock_client = MagicMock()
        mock_client.list_employees = mock_list_employees
        mock_client.list_departments = mock_list_departments
        mock_make.return_value = mock_client
        connector._http_client = mock_client

        result = await connector.sync()

    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2  # 1 emp + 1 dept
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_empty_returns_completed():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )

    async def mock_list_employees(cursor=None, limit=100):
        return {"data": [], "total": 0}

    async def mock_list_departments():
        return {"data": [], "total": 0}

    with patch.object(connector, "_make_client") as mock_make:
        mock_client = MagicMock()
        mock_client.list_employees = mock_list_employees
        mock_client.list_departments = mock_list_departments
        mock_make.return_value = mock_client
        connector._http_client = mock_client

        result = await connector.sync()

    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_employee_api_error_returns_failed():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )

    async def mock_list_employees(cursor=None, limit=100):
        raise RipplingNetworkError("network timeout")

    with patch.object(connector, "_make_client") as mock_make:
        mock_client = MagicMock()
        mock_client.list_employees = mock_list_employees
        mock_make.return_value = mock_client
        connector._http_client = mock_client

        result = await connector.sync()

    assert result.status == SyncStatus.FAILED
    assert "network timeout" in result.message


@pytest.mark.asyncio
async def test_sync_pagination_exhausts_all_pages():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )

    call_count = 0

    async def mock_list_employees(cursor=None, limit=100):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"data": [SAMPLE_EMPLOYEE_CAMEL], "next_cursor": "100", "total": 2}
        return {"data": [SAMPLE_EMPLOYEE_SNAKE], "total": 2}

    async def mock_list_departments():
        return {"data": [], "total": 0}

    with patch.object(connector, "_make_client") as mock_make:
        mock_client = MagicMock()
        mock_client.list_employees = mock_list_employees
        mock_client.list_departments = mock_list_departments
        mock_make.return_value = mock_client
        connector._http_client = mock_client

        result = await connector.sync()

    assert result.documents_synced == 2
    assert call_count == 2  # two pages fetched


# ── 13. RipplingConnector.list_employees (2 tests) ───────────────────────────


@pytest.mark.asyncio
async def test_list_employees_returns_all_items():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )

    async def mock_list_employees(cursor=None, limit=100):
        return {"data": [SAMPLE_EMPLOYEE_CAMEL, SAMPLE_EMPLOYEE_SNAKE], "total": 2}

    mock_client = MagicMock()
    mock_client.list_employees = mock_list_employees
    connector._http_client = mock_client

    employees = await connector.list_employees()
    assert len(employees) == 2
    assert employees[0]["id"] == "emp_001"


@pytest.mark.asyncio
async def test_list_employees_empty():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )

    async def mock_list_employees(cursor=None, limit=100):
        return {"data": [], "total": 0}

    mock_client = MagicMock()
    mock_client.list_employees = mock_list_employees
    connector._http_client = mock_client

    employees = await connector.list_employees()
    assert employees == []


# ── 14. RipplingConnector.list_departments (2 tests) ─────────────────────────


@pytest.mark.asyncio
async def test_list_departments_returns_items():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )

    async def mock_list_departments():
        return {"data": [SAMPLE_DEPARTMENT], "total": 1}

    mock_client = MagicMock()
    mock_client.list_departments = mock_list_departments
    connector._http_client = mock_client

    departments = await connector.list_departments()
    assert len(departments) == 1
    assert departments[0]["name"] == "Engineering"


@pytest.mark.asyncio
async def test_list_departments_empty():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )

    async def mock_list_departments():
        return {"data": [], "total": 0}

    mock_client = MagicMock()
    mock_client.list_departments = mock_list_departments
    connector._http_client = mock_client

    departments = await connector.list_departments()
    assert departments == []


# ── 15. RipplingConnector.list_teams (2 tests) ────────────────────────────────


@pytest.mark.asyncio
async def test_list_teams_returns_items():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )

    async def mock_list_teams():
        return {"data": [SAMPLE_TEAM], "total": 1}

    mock_client = MagicMock()
    mock_client.list_teams = mock_list_teams
    connector._http_client = mock_client

    teams = await connector.list_teams()
    assert len(teams) == 1
    assert teams[0]["name"] == "Platform Team"


@pytest.mark.asyncio
async def test_list_teams_empty():
    connector = RipplingConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )

    async def mock_list_teams():
        return {"data": [], "total": 0}

    mock_client = MagicMock()
    mock_client.list_teams = mock_list_teams
    connector._http_client = mock_client

    teams = await connector.list_teams()
    assert teams == []
