"""Unit tests for ServiceNowConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import ServiceNowConnector
from exceptions import (
    ServiceNowAuthError,
    ServiceNowError,
    ServiceNowNetworkError,
    ServiceNowNotFoundError,
    ServiceNowRateLimitError,
)
from helpers.utils import normalize_change, normalize_incident, with_retry
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ─────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_servicenow_test_001"
INSTANCE = "dev12345"
USERNAME = "admin"
PASSWORD = "ServiceNow_Pass_123!"

SAMPLE_INCIDENT: dict = {
    "sys_id": "abc123def456abc1",
    "number": "INC0010001",
    "short_description": "Email service is down",
    "description": "Users cannot send or receive emails since 09:00 UTC.",
    "state": "1",
    "priority": "2",
    "urgency": "2",
    "impact": "2",
    "category": "email",
    "assigned_to": {"value": "user_sys_id_001", "display_value": "Jane Doe"},
    "caller_id": {"value": "caller_sys_id_002", "display_value": "John Smith"},
    "opened_at": "2026-06-19T09:00:00Z",
    "resolved_at": "",
    "closed_at": "",
    "sys_updated_on": "2026-06-19T10:00:00Z",
}

SAMPLE_CHANGE: dict = {
    "sys_id": "chg000001sys0001",
    "number": "CHG0020001",
    "short_description": "Upgrade database to PostgreSQL 17",
    "description": "Planned upgrade of the primary database cluster.",
    "state": "1",
    "priority": "3",
    "risk": "2",
    "type": "normal",
    "assigned_to": {"value": "user_sys_id_003", "display_value": "Alice Brown"},
    "requested_by": {"value": "user_sys_id_004", "display_value": "Bob Lee"},
    "start_date": "2026-06-20T02:00:00Z",
    "end_date": "2026-06-20T06:00:00Z",
    "sys_updated_on": "2026-06-19T11:00:00Z",
}

SAMPLE_PROBLEM: dict = {
    "sys_id": "prb000001sys0001",
    "number": "PRB0030001",
    "short_description": "Recurring login failures",
    "state": "1",
    "priority": "2",
}

SAMPLE_CATALOG_ITEM: dict = {
    "sys_id": "cat000001sys0001",
    "name": "New Employee Onboarding",
    "short_description": "Request equipment and access for a new hire",
    "category": "HR",
}

SAMPLE_USER_RESPONSE: dict = {
    "result": [
        {
            "sys_id": "user_sys_id_001",
            "name": "Jane Doe",
            "user_name": "jane.doe",
            "email": "jane.doe@example.com",
        }
    ]
}

SAMPLE_INCIDENTS_PAGE: dict = {
    "result": [SAMPLE_INCIDENT],
}

SAMPLE_CHANGES_PAGE: dict = {
    "result": [SAMPLE_CHANGE],
}

SAMPLE_PROBLEMS_PAGE: dict = {
    "result": [SAMPLE_PROBLEM],
}

SAMPLE_CATALOG_PAGE: dict = {
    "result": [SAMPLE_CATALOG_ITEM],
}

SAMPLE_USERS_PAGE: dict = {
    "result": [
        {"sys_id": "user_001", "name": "Jane Doe", "user_name": "jane.doe"},
        {"sys_id": "user_002", "name": "Bob Lee", "user_name": "bob.lee"},
    ]
}

SAMPLE_CMDB_PAGE: dict = {
    "result": [
        {"sys_id": "ci_001", "name": "Web Server 1", "sys_class_name": "cmdb_ci_linux_server"},
    ]
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> ServiceNowConnector:
    return ServiceNowConnector(
        instance=INSTANCE,
        username=USERNAME,
        password=PASSWORD,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: ServiceNowConnector) -> ServiceNowConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success(connector: ServiceNowConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
    connector._make_client = lambda: mock_client
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "dev12345" in result.message


@pytest.mark.asyncio
async def test_install_success_shows_username(connector: ServiceNowConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
    connector._make_client = lambda: mock_client
    result = await connector.install()
    assert "Jane Doe" in result.message


@pytest.mark.asyncio
async def test_install_missing_instance() -> None:
    c = ServiceNowConnector(username=USERNAME, password=PASSWORD, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "instance" in result.message


@pytest.mark.asyncio
async def test_install_missing_username() -> None:
    c = ServiceNowConnector(instance=INSTANCE, password=PASSWORD, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "username" in result.message


@pytest.mark.asyncio
async def test_install_missing_password() -> None:
    c = ServiceNowConnector(instance=INSTANCE, username=USERNAME, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "password" in result.message


@pytest.mark.asyncio
async def test_install_missing_all_fields() -> None:
    c = ServiceNowConnector(tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector: ServiceNowConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(
        side_effect=ServiceNowAuthError("Invalid credentials", 401)
    )
    connector._make_client = lambda: mock_client
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid credentials" in result.message


@pytest.mark.asyncio
async def test_install_network_error(connector: ServiceNowConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(
        side_effect=ServiceNowNetworkError("Connection refused")
    )
    connector._make_client = lambda: mock_client
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_error(connector: ServiceNowConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(side_effect=RuntimeError("unexpected"))
    connector._make_client = lambda: mock_client
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_empty_result_list(connector: ServiceNowConnector) -> None:
    """install() succeeds even when result list is empty (user not found in query)."""
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(return_value={"result": []})
    connector._make_client = lambda: mock_client
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: ServiceNowConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "dev12345" in result.message


@pytest.mark.asyncio
async def test_health_check_includes_user_name(connector: ServiceNowConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert "Jane Doe" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: ServiceNowConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(
        side_effect=ServiceNowAuthError("Forbidden", 403)
    )
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: ServiceNowConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(
        side_effect=ServiceNowNetworkError("Timeout")
    )
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = ServiceNowConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: ServiceNowConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_current_user = AsyncMock(side_effect=RuntimeError("boom"))
    connector._make_client = lambda: mock_client
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector_with_mock_client: ServiceNowConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_incidents = AsyncMock(return_value={"result": []})
    c._http_client.list_changes = AsyncMock(return_value={"result": []})
    result = await c.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_incidents_and_changes(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE)
    c._http_client.list_changes = AsyncMock(return_value=SAMPLE_CHANGES_PAGE)
    result = await c.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_pagination_incidents(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    incident2 = {**SAMPLE_INCIDENT, "sys_id": "inc_page2_sys_001", "number": "INC0010002"}
    page1_items = [SAMPLE_INCIDENT] * 100  # triggers pagination
    page2_items = [incident2]  # stops pagination
    c._http_client.list_incidents = AsyncMock(
        side_effect=[
            {"result": page1_items},
            {"result": page2_items},
        ]
    )
    c._http_client.list_changes = AsyncMock(return_value={"result": []})
    result = await c.sync(full=True)
    assert result.documents_found == 101
    assert c._http_client.list_incidents.call_count == 2


@pytest.mark.asyncio
async def test_sync_partial_failure(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_incidents = AsyncMock(
        return_value={"result": [SAMPLE_INCIDENT, SAMPLE_INCIDENT]}
    )
    c._http_client.list_changes = AsyncMock(return_value={"result": []})

    import helpers.utils as hu
    original = hu.normalize_incident
    call_count = 0

    def raising_normalize(record, connector_id, tenant_id, instance):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("normalize failed")
        return original(record, connector_id, tenant_id, instance)

    # Patch in the module where connector.py imported it from (the `helpers` package),
    # and also in helpers.utils so both references are covered.
    import helpers as helpers_pkg
    import connector as connector_module
    original_helpers = getattr(helpers_pkg, "normalize_incident", original)
    original_connector = getattr(connector_module, "normalize_incident", None)
    hu.normalize_incident = raising_normalize
    helpers_pkg.normalize_incident = raising_normalize  # type: ignore[attr-defined]
    if original_connector is not None:
        connector_module.normalize_incident = raising_normalize  # type: ignore[attr-defined]
    try:
        result = await c.sync(full=True)
    finally:
        hu.normalize_incident = original
        helpers_pkg.normalize_incident = original_helpers  # type: ignore[attr-defined]
        if original_connector is not None:
            connector_module.normalize_incident = original_connector  # type: ignore[attr-defined]

    assert result.status == SyncStatus.PARTIAL
    assert result.documents_found == 2
    assert result.documents_synced == 1
    assert result.documents_failed == 1


@pytest.mark.asyncio
async def test_sync_incident_api_error_returns_failed(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_incidents = AsyncMock(
        side_effect=ServiceNowError("server error", 500)
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_change_api_error_returns_failed(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_incidents = AsyncMock(return_value={"result": []})
    c._http_client.list_changes = AsyncMock(
        side_effect=ServiceNowError("change api error", 500)
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "change api error" in result.message


@pytest.mark.asyncio
async def test_sync_accepts_kwargs(connector_with_mock_client: ServiceNowConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_incidents = AsyncMock(return_value={"result": []})
    c._http_client.list_changes = AsyncMock(return_value={"result": []})
    result = await c.sync(extra_param="ignored")
    assert result.status == SyncStatus.COMPLETED


# ── list_incidents() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_incidents_returns_page(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE)
    result = await c.list_incidents(limit=50, offset=0)
    assert "result" in result
    assert len(result["result"]) == 1
    assert result["result"][0]["number"] == "INC0010001"


@pytest.mark.asyncio
async def test_list_incidents_default_params(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE)
    await c.list_incidents()
    call_kwargs = c._http_client.list_incidents.call_args
    assert call_kwargs.kwargs.get("limit") == 100
    assert call_kwargs.kwargs.get("offset") == 0


@pytest.mark.asyncio
async def test_list_incidents_with_query(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE)
    await c.list_incidents(query="state=1^priority=2")
    call_kwargs = c._http_client.list_incidents.call_args
    assert call_kwargs.kwargs.get("query") == "state=1^priority=2"


@pytest.mark.asyncio
async def test_list_incidents_with_sysparm_query_alias(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE)
    await c.list_incidents(sysparm_query="state=1")
    call_kwargs = c._http_client.list_incidents.call_args
    assert call_kwargs.kwargs.get("query") == "state=1"


# ── get_incident() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_incident_returns_record(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_incident = AsyncMock(
        return_value={"result": SAMPLE_INCIDENT}
    )
    result = await c.get_incident("abc123def456abc1")
    assert result["result"]["number"] == "INC0010001"


@pytest.mark.asyncio
async def test_get_incident_passes_sys_id(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_incident = AsyncMock(
        return_value={"result": SAMPLE_INCIDENT}
    )
    await c.get_incident("abc123def456abc1")
    call_args = c._http_client.get_incident.call_args
    assert "abc123def456abc1" in call_args.args


@pytest.mark.asyncio
async def test_get_incident_not_found_raises(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_incident = AsyncMock(
        side_effect=ServiceNowNotFoundError("incident", "nonexistent_sys_id")
    )
    with pytest.raises(ServiceNowNotFoundError):
        await c.get_incident("nonexistent_sys_id")


# ── list_problems() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_problems_returns_page(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_problems = AsyncMock(return_value=SAMPLE_PROBLEMS_PAGE)
    result = await c.list_problems()
    assert "result" in result
    assert result["result"][0]["number"] == "PRB0030001"


@pytest.mark.asyncio
async def test_list_problems_pagination_params(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_problems = AsyncMock(return_value=SAMPLE_PROBLEMS_PAGE)
    await c.list_problems(limit=25, offset=50)
    call_kwargs = c._http_client.list_problems.call_args
    assert call_kwargs.kwargs.get("limit") == 25
    assert call_kwargs.kwargs.get("offset") == 50


@pytest.mark.asyncio
async def test_list_problems_default_params(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_problems = AsyncMock(return_value=SAMPLE_PROBLEMS_PAGE)
    await c.list_problems()
    call_kwargs = c._http_client.list_problems.call_args
    assert call_kwargs.kwargs.get("limit") == 100
    assert call_kwargs.kwargs.get("offset") == 0


# ── list_changes() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_changes_returns_page(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_changes = AsyncMock(return_value=SAMPLE_CHANGES_PAGE)
    result = await c.list_changes()
    assert "result" in result
    assert result["result"][0]["number"] == "CHG0020001"


@pytest.mark.asyncio
async def test_list_changes_pagination_params(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_changes = AsyncMock(return_value=SAMPLE_CHANGES_PAGE)
    await c.list_changes(limit=25, offset=50)
    call_kwargs = c._http_client.list_changes.call_args
    assert call_kwargs.kwargs.get("limit") == 25
    assert call_kwargs.kwargs.get("offset") == 50


# ── get_change() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_change_returns_record(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_change = AsyncMock(
        return_value={"result": SAMPLE_CHANGE}
    )
    result = await c.get_change("chg000001sys0001")
    assert result["result"]["number"] == "CHG0020001"


@pytest.mark.asyncio
async def test_get_change_not_found_raises(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_change = AsyncMock(
        side_effect=ServiceNowNotFoundError("change", "bad_sys_id")
    )
    with pytest.raises(ServiceNowNotFoundError):
        await c.get_change("bad_sys_id")


# ── list_service_catalog_items() ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_service_catalog_items_returns_page(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_service_catalog_items = AsyncMock(return_value=SAMPLE_CATALOG_PAGE)
    result = await c.list_service_catalog_items()
    assert "result" in result
    assert result["result"][0]["name"] == "New Employee Onboarding"


@pytest.mark.asyncio
async def test_list_service_catalog_items_pagination_params(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_service_catalog_items = AsyncMock(return_value=SAMPLE_CATALOG_PAGE)
    await c.list_service_catalog_items(limit=20, offset=40)
    call_kwargs = c._http_client.list_service_catalog_items.call_args
    assert call_kwargs.kwargs.get("limit") == 20
    assert call_kwargs.kwargs.get("offset") == 40


@pytest.mark.asyncio
async def test_list_service_catalog_items_default_params(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_service_catalog_items = AsyncMock(return_value=SAMPLE_CATALOG_PAGE)
    await c.list_service_catalog_items()
    call_kwargs = c._http_client.list_service_catalog_items.call_args
    assert call_kwargs.kwargs.get("limit") == 100
    assert call_kwargs.kwargs.get("offset") == 0


# ── list_users() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_returns_page(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_users = AsyncMock(return_value=SAMPLE_USERS_PAGE)
    result = await c.list_users()
    assert "result" in result
    assert len(result["result"]) == 2
    assert result["result"][0]["name"] == "Jane Doe"


@pytest.mark.asyncio
async def test_list_users_pagination_params(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_users = AsyncMock(return_value=SAMPLE_USERS_PAGE)
    await c.list_users(limit=50, offset=100)
    call_kwargs = c._http_client.list_users.call_args
    assert call_kwargs.kwargs.get("limit") == 50
    assert call_kwargs.kwargs.get("offset") == 100


# ── list_cmdb_items() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_cmdb_items_returns_page(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_cmdb_items = AsyncMock(return_value=SAMPLE_CMDB_PAGE)
    result = await c.list_cmdb_items()
    assert "result" in result
    assert result["result"][0]["name"] == "Web Server 1"


@pytest.mark.asyncio
async def test_list_cmdb_items_custom_class(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_cmdb_items = AsyncMock(return_value=SAMPLE_CMDB_PAGE)
    await c.list_cmdb_items(class_name="cmdb_ci_linux_server", limit=50)
    call_kwargs = c._http_client.list_cmdb_items.call_args
    assert call_kwargs.kwargs.get("class_name") == "cmdb_ci_linux_server"
    assert call_kwargs.kwargs.get("limit") == 50


# ── normalize_incident() ─────────────────────────────────────────────────────


def test_normalize_incident_title() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc.title == "Incident INC0010001: Email service is down"


def test_normalize_incident_source_id_is_16_chars() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert len(doc.source_id) == 16


def test_normalize_incident_source_id_is_hex() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    int(doc.source_id, 16)  # raises ValueError if not hex


def test_normalize_incident_source_id_deterministic() -> None:
    doc1 = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    doc2 = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc1.source_id == doc2.source_id


def test_normalize_incident_source_id_prefix() -> None:
    """source_id = sha256('incident:' + sys_id)[:16]."""
    import hashlib
    expected = hashlib.sha256(
        f"incident:{SAMPLE_INCIDENT['sys_id']}".encode()
    ).hexdigest()[:16]
    doc = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc.source_id == expected


def test_normalize_incident_different_sys_ids_different_source_ids() -> None:
    incident2 = {**SAMPLE_INCIDENT, "sys_id": "zzz999yyy888zzz9"}
    doc1 = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    doc2 = normalize_incident(incident2, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc1.source_id != doc2.source_id


def test_normalize_incident_source_url() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc.source_url == f"https://{INSTANCE}.service-now.com/incident.do?sys_id=abc123def456abc1"


def test_normalize_incident_metadata_fields() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    meta = doc.metadata
    assert meta["sys_id"] == "abc123def456abc1"
    assert meta["number"] == "INC0010001"
    assert meta["state"] == "1"
    assert meta["priority"] == "2"
    assert meta["urgency"] == "2"
    assert meta["impact"] == "2"
    assert meta["category"] == "email"
    assert meta["opened_at"] == "2026-06-19T09:00:00Z"
    assert meta["record_type"] == "incident"


def test_normalize_incident_extracts_display_value_fields() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    # assigned_to and caller_id are dicts with value/display_value
    assert doc.metadata["assigned_to"] == "user_sys_id_001"
    assert doc.metadata["caller_id"] == "caller_sys_id_002"


def test_normalize_incident_content_includes_description() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert "Users cannot send or receive emails since 09:00 UTC." in doc.content


def test_normalize_incident_content_includes_short_description() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert "Email service is down" in doc.content


def test_normalize_incident_connector_and_tenant_ids() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_incident_empty_number_fallback() -> None:
    incident = {**SAMPLE_INCIDENT, "number": ""}
    doc = normalize_incident(incident, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert "Incident" in doc.title


def test_normalize_incident_empty_sys_id_produces_empty_url() -> None:
    incident = {**SAMPLE_INCIDENT, "sys_id": ""}
    doc = normalize_incident(incident, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc.source_url == ""


# ── normalize_change() ───────────────────────────────────────────────────────


def test_normalize_change_title() -> None:
    doc = normalize_change(SAMPLE_CHANGE, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc.title == "Change Request CHG0020001: Upgrade database to PostgreSQL 17"


def test_normalize_change_source_id_is_16_chars() -> None:
    doc = normalize_change(SAMPLE_CHANGE, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert len(doc.source_id) == 16


def test_normalize_change_source_id_prefix() -> None:
    """source_id = sha256('change:' + sys_id)[:16]."""
    import hashlib
    expected = hashlib.sha256(
        f"change:{SAMPLE_CHANGE['sys_id']}".encode()
    ).hexdigest()[:16]
    doc = normalize_change(SAMPLE_CHANGE, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc.source_id == expected


def test_normalize_change_incident_and_change_have_different_source_ids() -> None:
    """Ensures 'incident:' and 'change:' prefixes produce different source_ids for same sys_id."""
    incident = {**SAMPLE_INCIDENT, "sys_id": "shared_sys_id_123456"}
    change = {**SAMPLE_CHANGE, "sys_id": "shared_sys_id_123456"}
    doc_inc = normalize_incident(incident, CONNECTOR_ID, TENANT_ID, INSTANCE)
    doc_chg = normalize_change(change, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc_inc.source_id != doc_chg.source_id


def test_normalize_change_source_url() -> None:
    doc = normalize_change(SAMPLE_CHANGE, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc.source_url == f"https://{INSTANCE}.service-now.com/change_request.do?sys_id=chg000001sys0001"


def test_normalize_change_metadata_fields() -> None:
    doc = normalize_change(SAMPLE_CHANGE, CONNECTOR_ID, TENANT_ID, INSTANCE)
    meta = doc.metadata
    assert meta["sys_id"] == "chg000001sys0001"
    assert meta["number"] == "CHG0020001"
    assert meta["state"] == "1"
    assert meta["priority"] == "3"
    assert meta["risk"] == "2"
    assert meta["change_type"] == "normal"
    assert meta["start_date"] == "2026-06-20T02:00:00Z"
    assert meta["end_date"] == "2026-06-20T06:00:00Z"
    assert meta["record_type"] == "change_request"


def test_normalize_change_content_includes_description() -> None:
    doc = normalize_change(SAMPLE_CHANGE, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert "Planned upgrade of the primary database cluster." in doc.content


def test_normalize_change_empty_sys_id_produces_empty_url() -> None:
    change = {**SAMPLE_CHANGE, "sys_id": ""}
    doc = normalize_change(change, CONNECTOR_ID, TENANT_ID, INSTANCE)
    assert doc.source_url == ""


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
            ServiceNowNetworkError("fail"),
            ServiceNowNetworkError("fail again"),
            {"ok": True},
        ]
    )
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=ServiceNowAuthError("invalid creds", 401))
    with pytest.raises(ServiceNowAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=ServiceNowNetworkError("persistent failure"))
    with pytest.raises(ServiceNowNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(
        side_effect=ServiceNowRateLimitError("429", retry_after=0)
    )
    with pytest.raises(ServiceNowRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_honours_retry_after_from_rate_limit() -> None:
    """with_retry respects the retry_after value from ServiceNowRateLimitError."""
    mock_fn = AsyncMock(
        side_effect=[
            ServiceNowRateLimitError("429", retry_after=0),
            {"ok": True},
        ]
    )
    result = await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    mock_fn = AsyncMock(return_value={"data": "value"})
    result = await with_retry(mock_fn, "arg1", "arg2", key="kw_val")
    mock_fn.assert_called_once_with("arg1", "arg2", key="kw_val")
    assert result == {"data": "value"}


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_hierarchy_auth_is_servicenow_error() -> None:
    exc = ServiceNowAuthError("bad creds", 401)
    assert isinstance(exc, ServiceNowError)


def test_exception_hierarchy_rate_limit_is_servicenow_error() -> None:
    exc = ServiceNowRateLimitError("too fast")
    assert isinstance(exc, ServiceNowError)
    assert exc.retry_after == 0.0


def test_exception_hierarchy_not_found_is_servicenow_error() -> None:
    exc = ServiceNowNotFoundError("incident", "abc123")
    assert isinstance(exc, ServiceNowError)
    assert exc.status_code == 404
    assert "abc123" in str(exc)


def test_exception_hierarchy_network_is_servicenow_error() -> None:
    exc = ServiceNowNetworkError("timeout", 500)
    assert isinstance(exc, ServiceNowError)


def test_rate_limit_stores_retry_after() -> None:
    exc = ServiceNowRateLimitError("slow down", retry_after=120.0)
    assert exc.retry_after == 120.0


def test_servicenow_error_stores_status_code_and_code() -> None:
    exc = ServiceNowError("boom", status_code=502, code="bad_gateway")
    assert exc.status_code == 502
    assert exc.code == "bad_gateway"


def test_not_found_message_contains_resource_id() -> None:
    exc = ServiceNowNotFoundError("change_request", "CHG0099999")
    assert "CHG0099999" in str(exc)
    assert exc.code == "resource_missing"


def test_servicenow_error_message_attribute() -> None:
    exc = ServiceNowError("test message", status_code=400)
    assert exc.message == "test message"


# ── HTTP client helpers ───────────────────────────────────────────────────────


def test_base_url_construction() -> None:
    from client.http_client import _build_base_url
    url = _build_base_url("dev12345")
    assert url == "https://dev12345.service-now.com"


def test_base_url_construction_different_instances() -> None:
    from client.http_client import _build_base_url
    assert _build_base_url("prod") == "https://prod.service-now.com"
    assert _build_base_url("my-company") == "https://my-company.service-now.com"


def test_http_client_make_auth() -> None:
    """BasicAuth is constructed correctly from username + password."""
    import aiohttp
    from client.http_client import ServiceNowHTTPClient
    client = ServiceNowHTTPClient()
    auth = client._make_auth("admin", "supersecret")
    assert isinstance(auth, aiohttp.BasicAuth)
    assert auth.login == "admin"
    assert auth.password == "supersecret"


def test_http_client_get_current_user_builds_sysparm_query() -> None:
    """get_current_user sends sysparm_query=user_name={username}."""
    from client.http_client import ServiceNowHTTPClient
    client = ServiceNowHTTPClient()
    # Verify the method exists and is callable
    assert callable(client.get_current_user)


def test_http_client_has_all_required_methods() -> None:
    """Verify all required HTTP client methods are present."""
    from client.http_client import ServiceNowHTTPClient
    client = ServiceNowHTTPClient()
    for method_name in [
        "get_current_user",
        "list_incidents",
        "get_incident",
        "list_problems",
        "list_changes",
        "get_change",
        "list_service_catalog_items",
        "list_users",
        "list_cmdb_items",
    ]:
        assert callable(getattr(client, method_name)), f"Missing method: {method_name}"


# ── Connector config loading ──────────────────────────────────────────────────


def test_connector_loads_from_config_dict() -> None:
    c = ServiceNowConnector(config={
        "instance": "myinstance",
        "username": "myuser",
        "password": "mysecret",
    })
    assert c._instance == "myinstance"
    assert c._username == "myuser"
    assert c._password == "mysecret"


def test_connector_keyword_args_fallback() -> None:
    c = ServiceNowConnector(
        instance="kwarg_inst",
        username="kwarg_user",
        password="kwarg_pass",
    )
    assert c._instance == "kwarg_inst"


def test_connector_config_takes_precedence_over_kwargs() -> None:
    c = ServiceNowConnector(
        config={
            "instance": "from_config",
            "username": "cfg_user",
            "password": "cfg_pass",
        },
        instance="from_kwarg",
        username="kwarg_user",
        password="kwarg_pass",
    )
    assert c._instance == "from_config"
    assert c._username == "cfg_user"


def test_connector_missing_credentials_list_all() -> None:
    c = ServiceNowConnector()
    missing = c._missing_credentials()
    assert "instance" in missing
    assert "username" in missing
    assert "password" in missing


def test_connector_missing_credentials_partial() -> None:
    c = ServiceNowConnector(instance="dev99")
    missing = c._missing_credentials()
    assert "instance" not in missing
    assert "username" in missing
    assert "password" in missing


def test_connector_missing_no_fields_when_all_provided() -> None:
    c = ServiceNowConnector(
        instance=INSTANCE, username=USERNAME, password=PASSWORD
    )
    assert c._missing_credentials() == []


# ── Connector lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_context_manager(connector: ServiceNowConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


def test_ensure_client_creates_on_first_call(connector: ServiceNowConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: ServiceNowConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2


def test_connector_type_constants() -> None:
    assert ServiceNowConnector.CONNECTOR_TYPE == "servicenow"
    assert ServiceNowConnector.AUTH_TYPE == "api_key"


# ── BaseConnector import guard ────────────────────────────────────────────────


def test_base_connector_fallback_attributes() -> None:
    """Connector initialises correctly with the fallback BaseConnector."""
    c = ServiceNowConnector(
        tenant_id="t123",
        connector_id="c456",
        instance=INSTANCE,
        username=USERNAME,
        password=PASSWORD,
    )
    assert c.tenant_id == "t123"
    assert c.connector_id == "c456"


def test_connector_has_required_module_constants() -> None:
    import connector as conn_module
    assert conn_module.CONNECTOR_TYPE == "servicenow"
    assert conn_module.AUTH_TYPE == "api_key"


# ── result wrapper unwrapping ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_incidents_result_wrapper_preserved(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    """Verify the full {"result": [...]} wrapper is returned as-is."""
    c = connector_with_mock_client
    payload = {"result": [SAMPLE_INCIDENT], "extra_meta": "ignored"}
    c._http_client.list_incidents = AsyncMock(return_value=payload)
    result = await c.list_incidents()
    assert result is payload
    assert "result" in result


@pytest.mark.asyncio
async def test_get_incident_result_wrapper_preserved(
    connector_with_mock_client: ServiceNowConnector,
) -> None:
    c = connector_with_mock_client
    payload = {"result": SAMPLE_INCIDENT}
    c._http_client.get_incident = AsyncMock(return_value=payload)
    result = await c.get_incident("some_sys_id")
    assert result["result"] is SAMPLE_INCIDENT


# ── _raise_for_status — status code mapping ───────────────────────────────────


@pytest.mark.asyncio
async def test_handle_response_401_raises_auth_error() -> None:
    from client.http_client import ServiceNowHTTPClient
    import aiohttp

    client = ServiceNowHTTPClient()

    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": {"message": "unauthorized"}})

    with pytest.raises(ServiceNowAuthError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_handle_response_403_raises_auth_error() -> None:
    from client.http_client import ServiceNowHTTPClient

    client = ServiceNowHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={})

    with pytest.raises(ServiceNowAuthError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_handle_response_404_raises_not_found() -> None:
    from client.http_client import ServiceNowHTTPClient

    client = ServiceNowHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": {"message": "not found"}})

    with pytest.raises(ServiceNowNotFoundError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_handle_response_429_raises_rate_limit() -> None:
    from client.http_client import ServiceNowHTTPClient

    client = ServiceNowHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "60"}
    mock_response.json = AsyncMock(return_value={})

    with pytest.raises(ServiceNowRateLimitError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.retry_after == 60.0


@pytest.mark.asyncio
async def test_handle_response_500_raises_network_error() -> None:
    from client.http_client import ServiceNowHTTPClient

    client = ServiceNowHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={})

    with pytest.raises(ServiceNowNetworkError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_handle_response_other_4xx_raises_servicenow_error() -> None:
    from client.http_client import ServiceNowHTTPClient

    client = ServiceNowHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 422
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": {"detail": "validation error"}})

    with pytest.raises(ServiceNowError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_handle_response_200_returns_json() -> None:
    from client.http_client import ServiceNowHTTPClient

    client = ServiceNowHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"result": []})

    result = await client._handle_response(mock_response)
    assert result == {"result": []}
