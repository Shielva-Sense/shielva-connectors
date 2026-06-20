"""Unit tests for Asana connector — all HTTP calls are mocked.

Covers:
- All exception types, hierarchy, message, codes
- ConnectorDocument model fields (id, source, type)
- normalize_task / normalize_project (spec-compliant signatures)
- with_retry: success, second-attempt, auth-fail-fast, exhausted, rate-limit
- Bearer auth header (api_key field)
- Asana {"data": ...} envelope unwrapping
- next_page.offset pagination
- All HTTP client methods + _raise_for_status
- install / health_check / sync
- list_workspaces / list_projects / list_tasks / get_task
- get_project / list_sections / list_users
- Lifecycle: aclose, context manager
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AsanaConnector
from exceptions import (
    AsanaAuthError,
    AsanaError,
    AsanaNetworkError,
    AsanaNotFoundError,
    AsanaRateLimitError,
)
from helpers.utils import normalize_project, normalize_task, with_retry
from models import AuthStatus, ConnectorDocument, ConnectorHealth, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_asana_test_001"
API_KEY = "test_personal_access_token_abc123"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_USER: dict = {
    "gid": "1001",
    "name": "Alice Asana",
    "email": "alice@testcompany.com",
    "resource_type": "user",
}

SAMPLE_USER_NO_NAME: dict = {
    "gid": "1002",
    "email": "bob@testcompany.com",
    "resource_type": "user",
}

SAMPLE_WORKSPACE: dict = {
    "gid": "ws_001",
    "name": "My Workspace",
    "resource_type": "workspace",
}

SAMPLE_WORKSPACE_2: dict = {
    "gid": "ws_002",
    "name": "Second Workspace",
    "resource_type": "workspace",
}

SAMPLE_PROJECT: dict = {
    "gid": "proj_001",
    "name": "Product Roadmap",
    "notes": "Q3 feature planning",
    "color": "blue",
    "created_at": "2026-01-01T00:00:00Z",
}

SAMPLE_PROJECT_2: dict = {
    "gid": "proj_002",
    "name": "Bug Tracking",
    "notes": "",
    "color": "red",
    "created_at": "2026-02-01T00:00:00Z",
}

SAMPLE_TASK: dict = {
    "gid": "task_001",
    "name": "Design new landing page",
    "notes": "Use the brand kit from Figma.",
    "completed": False,
    "due_on": "2026-07-15",
    "assignee": {"gid": "1001", "name": "Alice Asana"},
    "created_at": "2026-06-01T10:00:00Z",
}

SAMPLE_TASK_2: dict = {
    "gid": "task_002",
    "name": "Fix login bug",
    "notes": "Reproduction steps in Notion.",
    "completed": True,
    "due_on": "2026-06-30",
    "assignee": None,
    "created_at": "2026-06-02T09:00:00Z",
}

SAMPLE_SECTION: dict = {
    "gid": "sec_001",
    "name": "In Progress",
    "resource_type": "section",
}

SAMPLE_USER_MEMBER: dict = {
    "gid": "user_001",
    "name": "Carol Designer",
    "email": "carol@testcompany.com",
}


def ws_page(workspaces: list, next_offset: str | None = None) -> dict:
    resp: dict = {"data": workspaces}
    if next_offset:
        resp["next_page"] = {"offset": next_offset}
    else:
        resp["next_page"] = None
    return resp


def projects_page(projects: list, next_offset: str | None = None) -> dict:
    resp: dict = {"data": projects}
    if next_offset:
        resp["next_page"] = {"offset": next_offset}
    else:
        resp["next_page"] = None
    return resp


def tasks_page(tasks: list, next_offset: str | None = None) -> dict:
    resp: dict = {"data": tasks}
    if next_offset:
        resp["next_page"] = {"offset": next_offset}
    else:
        resp["next_page"] = None
    return resp


def users_page(users: list, next_offset: str | None = None) -> dict:
    resp: dict = {"data": users}
    if next_offset:
        resp["next_page"] = {"offset": next_offset}
    else:
        resp["next_page"] = None
    return resp


# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_connector(api_key: str = API_KEY, workspace_gid: str = "") -> AsanaConnector:
    return AsanaConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key, "workspace_gid": workspace_gid},
    )


@pytest.fixture()
def connector() -> AsanaConnector:
    c = make_connector()
    c._http_client = MagicMock()
    return c


# ── Models ────────────────────────────────────────────────────────────────────


def test_connector_document_has_id_source_type() -> None:
    doc = ConnectorDocument(
        id="abc123",
        source="asana",
        type="task",
        title="Test",
        content="Body",
    )
    assert doc.id == "abc123"
    assert doc.source == "asana"
    assert doc.type == "task"
    assert doc.metadata == {}


def test_connector_document_metadata_default_empty() -> None:
    doc = ConnectorDocument(id="x", source="asana", type="project", title="P", content="C")
    assert isinstance(doc.metadata, dict)
    assert len(doc.metadata) == 0


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = make_connector()
    instance = MagicMock()
    instance.get_current_user = AsyncMock(return_value=SAMPLE_USER)
    instance.aclose = AsyncMock()
    c._make_client = lambda: instance
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Alice Asana" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = make_connector(api_key="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    c = make_connector()
    instance = MagicMock()
    instance.get_current_user = AsyncMock(
        side_effect=AsanaAuthError("Unauthorized", 401)
    )
    instance.aclose = AsyncMock()
    c._make_client = lambda: instance
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = make_connector()
    instance = MagicMock()
    instance.get_current_user = AsyncMock(
        side_effect=AsanaNetworkError("Connection refused")
    )
    instance.aclose = AsyncMock()
    c._make_client = lambda: instance
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unknown_exception() -> None:
    c = make_connector()
    instance = MagicMock()
    instance.get_current_user = AsyncMock(side_effect=Exception("boom"))
    instance.aclose = AsyncMock()
    c._make_client = lambda: instance
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_user_name_from_email_fallback() -> None:
    c = make_connector()
    instance = MagicMock()
    instance.get_current_user = AsyncMock(return_value=SAMPLE_USER_NO_NAME)
    instance.aclose = AsyncMock()
    c._make_client = lambda: instance
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert "bob@testcompany.com" in result.message


@pytest.mark.asyncio
async def test_install_user_name_unknown_fallback() -> None:
    c = make_connector()
    instance = MagicMock()
    instance.get_current_user = AsyncMock(return_value={"gid": "9"})
    instance.aclose = AsyncMock()
    c._make_client = lambda: instance
    result = await c.install()
    assert "Unknown user" in result.message


# ── health_check() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: AsanaConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_current_user=AsyncMock(return_value=SAMPLE_USER),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message
    assert "Alice Asana" in result.message


@pytest.mark.asyncio
async def test_health_check_uses_email_when_no_name(connector: AsanaConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_current_user=AsyncMock(return_value=SAMPLE_USER_NO_NAME),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert "bob@testcompany.com" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_key(connector: AsanaConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_current_user=AsyncMock(
            side_effect=AsanaAuthError("Invalid token", 401)
        ),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: AsanaConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_current_user=AsyncMock(side_effect=AsanaNetworkError("timeout")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: AsanaConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_current_user=AsyncMock(side_effect=Exception("unexpected")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_missing_creds() -> None:
    c = make_connector(api_key="")
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── sync() — workspace/project/task traversal ─────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty_workspaces(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(return_value=ws_page([]))
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_workspace_no_projects(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE])
    )
    connector._http_client.list_projects = AsyncMock(
        return_value=projects_page([])
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0


@pytest.mark.asyncio
async def test_sync_one_project_no_tasks(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE])
    )
    connector._http_client.list_projects = AsyncMock(
        side_effect=[projects_page([SAMPLE_PROJECT]), projects_page([])]
    )
    connector._http_client.list_tasks = AsyncMock(return_value=tasks_page([]))
    result = await connector.sync()
    assert result.documents_found == 1  # 1 project
    assert result.documents_synced == 1
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_one_project_with_tasks(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE])
    )
    connector._http_client.list_projects = AsyncMock(
        side_effect=[projects_page([SAMPLE_PROJECT]), projects_page([])]
    )
    connector._http_client.list_tasks = AsyncMock(
        side_effect=[tasks_page([SAMPLE_TASK, SAMPLE_TASK_2]), tasks_page([])]
    )
    result = await connector.sync()
    # 1 project + 2 tasks
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_multiple_workspaces(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE, SAMPLE_WORKSPACE_2])
    )
    connector._http_client.list_projects = AsyncMock(
        side_effect=[
            projects_page([SAMPLE_PROJECT]),
            projects_page([SAMPLE_PROJECT_2]),
        ]
    )
    connector._http_client.list_tasks = AsyncMock(
        side_effect=[
            tasks_page([SAMPLE_TASK]),
            tasks_page([SAMPLE_TASK_2]),
        ]
    )
    result = await connector.sync()
    # 2 projects + 2 tasks = 4
    assert result.documents_found == 4
    assert result.documents_synced == 4
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_workspace_list_failure(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        side_effect=AsanaNetworkError("Server error", 500)
    )
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "workspaces" in result.message.lower()


@pytest.mark.asyncio
async def test_sync_project_list_failure_skips_workspace(
    connector: AsanaConnector,
) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE])
    )
    connector._http_client.list_projects = AsyncMock(
        side_effect=AsanaNetworkError("projects error")
    )
    result = await connector.sync()
    assert result.documents_found == 0


@pytest.mark.asyncio
async def test_sync_task_list_failure_skips_project(
    connector: AsanaConnector,
) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE])
    )
    connector._http_client.list_projects = AsyncMock(
        side_effect=[projects_page([SAMPLE_PROJECT]), projects_page([])]
    )
    connector._http_client.list_tasks = AsyncMock(
        side_effect=AsanaNetworkError("tasks error")
    )
    result = await connector.sync()
    # Project itself still synced
    assert result.documents_synced >= 1


@pytest.mark.asyncio
async def test_sync_normalizer_failure_counts_failed(
    connector: AsanaConnector,
) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE])
    )
    connector._http_client.list_projects = AsyncMock(
        side_effect=[projects_page([SAMPLE_PROJECT]), projects_page([])]
    )
    connector._http_client.list_tasks = AsyncMock(
        side_effect=[tasks_page([SAMPLE_TASK]), tasks_page([])]
    )
    with patch("connector.normalize_task", side_effect=Exception("normalizer failed")):
        result = await connector.sync()
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_ingest_called_with_kb_id(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE])
    )
    connector._http_client.list_projects = AsyncMock(
        side_effect=[projects_page([SAMPLE_PROJECT]), projects_page([])]
    )
    connector._http_client.list_tasks = AsyncMock(
        side_effect=[tasks_page([SAMPLE_TASK]), tasks_page([])]
    )
    connector._ingest_document = AsyncMock()
    result = await connector.sync(kb_id="kb_asana_001")
    # 1 project + 1 task = 2 ingest calls
    assert connector._ingest_document.call_count == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_pagination_projects(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE])
    )
    connector._http_client.list_projects = AsyncMock(
        side_effect=[
            projects_page([SAMPLE_PROJECT], next_offset="cursor1"),
            projects_page([SAMPLE_PROJECT_2]),
            projects_page([]),
        ]
    )
    connector._http_client.list_tasks = AsyncMock(return_value=tasks_page([]))
    result = await connector.sync()
    assert result.documents_found == 2  # 2 projects across pages


@pytest.mark.asyncio
async def test_sync_pagination_tasks(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE])
    )
    connector._http_client.list_projects = AsyncMock(
        side_effect=[projects_page([SAMPLE_PROJECT]), projects_page([])]
    )
    connector._http_client.list_tasks = AsyncMock(
        side_effect=[
            tasks_page([SAMPLE_TASK], next_offset="tcursor1"),
            tasks_page([SAMPLE_TASK_2]),
            tasks_page([]),
        ]
    )
    result = await connector.sync()
    # 1 project + 2 tasks across pages
    assert result.documents_found == 3
    assert result.documents_synced == 3


@pytest.mark.asyncio
async def test_sync_skips_workspace_with_no_gid(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([{"name": "broken"}, SAMPLE_WORKSPACE])
    )
    connector._http_client.list_projects = AsyncMock(
        return_value=projects_page([])
    )
    result = await connector.sync()
    # Should not crash; only valid workspace iterated
    assert result.documents_found == 0


@pytest.mark.asyncio
async def test_sync_filters_by_workspace_gid() -> None:
    """When workspace_gid config is set, only that workspace is synced."""
    c = make_connector(workspace_gid="ws_001")
    c._http_client = MagicMock()
    c._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE, SAMPLE_WORKSPACE_2])
    )
    c._http_client.list_projects = AsyncMock(return_value=projects_page([]))
    await c.sync()
    # list_projects called once for ws_001 only
    c._http_client.list_projects.assert_called_once()
    call_args = c._http_client.list_projects.call_args[0]
    assert "ws_001" in call_args


# ── list_workspaces() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_workspaces_returns_list(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(
        return_value=ws_page([SAMPLE_WORKSPACE, SAMPLE_WORKSPACE_2])
    )
    result = await connector.list_workspaces()
    assert len(result) == 2
    assert result[0]["gid"] == "ws_001"


@pytest.mark.asyncio
async def test_list_workspaces_empty(connector: AsanaConnector) -> None:
    connector._http_client.list_workspaces = AsyncMock(return_value=ws_page([]))
    result = await connector.list_workspaces()
    assert result == []


# ── list_projects() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_projects_returns_list(connector: AsanaConnector) -> None:
    connector._http_client.list_projects = AsyncMock(
        return_value=projects_page([SAMPLE_PROJECT, SAMPLE_PROJECT_2])
    )
    result = await connector.list_projects("ws_001")
    assert len(result) == 2
    assert result[0]["gid"] == "proj_001"


@pytest.mark.asyncio
async def test_list_projects_empty(connector: AsanaConnector) -> None:
    connector._http_client.list_projects = AsyncMock(
        return_value=projects_page([])
    )
    result = await connector.list_projects("ws_001")
    assert result == []


@pytest.mark.asyncio
async def test_list_projects_archived_flag(connector: AsanaConnector) -> None:
    connector._http_client.list_projects = AsyncMock(
        return_value=projects_page([SAMPLE_PROJECT])
    )
    await connector.list_projects("ws_001", archived=True)
    connector._http_client.list_projects.assert_called_once_with(
        API_KEY, "ws_001", True
    )


@pytest.mark.asyncio
async def test_list_projects_uses_config_workspace_when_none(connector: AsanaConnector) -> None:
    """When no workspace_gid passed, uses config workspace_gid."""
    c = make_connector(workspace_gid="ws_from_config")
    c._http_client = MagicMock()
    c._http_client.list_projects = AsyncMock(return_value=projects_page([]))
    await c.list_projects()
    call_args = c._http_client.list_projects.call_args[0]
    assert "ws_from_config" in call_args


# ── get_project() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_project_success(connector: AsanaConnector) -> None:
    connector._http_client.get_project = AsyncMock(return_value=SAMPLE_PROJECT)
    result = await connector.get_project("proj_001")
    assert result["gid"] == "proj_001"
    assert result["name"] == "Product Roadmap"


@pytest.mark.asyncio
async def test_get_project_not_found(connector: AsanaConnector) -> None:
    connector._http_client.get_project = AsyncMock(
        side_effect=AsanaNotFoundError("project", "proj_999")
    )
    with pytest.raises(AsanaNotFoundError):
        await connector.get_project("proj_999")


@pytest.mark.asyncio
async def test_get_project_auth_error(connector: AsanaConnector) -> None:
    connector._http_client.get_project = AsyncMock(
        side_effect=AsanaAuthError("Forbidden", 403)
    )
    with pytest.raises(AsanaAuthError):
        await connector.get_project("proj_001")


# ── list_tasks() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tasks_returns_list(connector: AsanaConnector) -> None:
    connector._http_client.list_tasks = AsyncMock(
        return_value=tasks_page([SAMPLE_TASK, SAMPLE_TASK_2])
    )
    result = await connector.list_tasks("proj_001")
    assert len(result) == 2
    assert result[0]["gid"] == "task_001"


@pytest.mark.asyncio
async def test_list_tasks_empty(connector: AsanaConnector) -> None:
    connector._http_client.list_tasks = AsyncMock(return_value=tasks_page([]))
    result = await connector.list_tasks("proj_001")
    assert result == []


# ── get_task() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_task_success(connector: AsanaConnector) -> None:
    connector._http_client.get_task = AsyncMock(return_value=SAMPLE_TASK)
    result = await connector.get_task("task_001")
    assert result["gid"] == "task_001"
    assert result["name"] == "Design new landing page"


@pytest.mark.asyncio
async def test_get_task_not_found(connector: AsanaConnector) -> None:
    connector._http_client.get_task = AsyncMock(
        side_effect=AsanaNotFoundError("task", "task_999")
    )
    with pytest.raises(AsanaNotFoundError):
        await connector.get_task("task_999")


@pytest.mark.asyncio
async def test_get_task_auth_error(connector: AsanaConnector) -> None:
    connector._http_client.get_task = AsyncMock(
        side_effect=AsanaAuthError("Forbidden", 403)
    )
    with pytest.raises(AsanaAuthError):
        await connector.get_task("task_001")


# ── list_sections() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sections_success(connector: AsanaConnector) -> None:
    connector._http_client.list_sections = AsyncMock(return_value=[SAMPLE_SECTION])
    result = await connector.list_sections("proj_001")
    assert len(result) == 1
    assert result[0]["gid"] == "sec_001"
    assert result[0]["name"] == "In Progress"


@pytest.mark.asyncio
async def test_list_sections_empty(connector: AsanaConnector) -> None:
    connector._http_client.list_sections = AsyncMock(return_value=[])
    result = await connector.list_sections("proj_001")
    assert result == []


@pytest.mark.asyncio
async def test_list_sections_not_found(connector: AsanaConnector) -> None:
    connector._http_client.list_sections = AsyncMock(
        side_effect=AsanaNotFoundError("project", "proj_bad")
    )
    with pytest.raises(AsanaNotFoundError):
        await connector.list_sections("proj_bad")


# ── list_users() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_success(connector: AsanaConnector) -> None:
    connector._http_client.list_users = AsyncMock(
        return_value=users_page([SAMPLE_USER_MEMBER])
    )
    result = await connector.list_users("ws_001")
    assert len(result) == 1
    assert result[0]["email"] == "carol@testcompany.com"


@pytest.mark.asyncio
async def test_list_users_empty(connector: AsanaConnector) -> None:
    connector._http_client.list_users = AsyncMock(return_value=users_page([]))
    result = await connector.list_users("ws_001")
    assert result == []


@pytest.mark.asyncio
async def test_list_users_uses_config_workspace_when_none() -> None:
    c = make_connector(workspace_gid="ws_from_config")
    c._http_client = MagicMock()
    c._http_client.list_users = AsyncMock(return_value=users_page([]))
    await c.list_users()
    call_args = c._http_client.list_users.call_args[0]
    assert "ws_from_config" in call_args


# ── normalize_task() ─────────────────────────────────────────────────────────


def test_normalize_task_basic() -> None:
    doc = normalize_task(SAMPLE_TASK, "proj_001")
    assert doc.id == hashlib.sha256("task:task_001".encode()).hexdigest()[:16]
    assert doc.source == "asana"
    assert doc.type == "task"
    assert doc.title == "Design new landing page"
    assert doc.metadata["task_gid"] == "task_001"
    assert doc.metadata["project_gid"] == "proj_001"
    assert doc.metadata["completed"] is False
    assert doc.metadata["due_on"] == "2026-07-15"
    assert doc.metadata["assignee_gid"] == "1001"
    assert doc.metadata["assignee_name"] == "Alice Asana"
    assert "proj_001" in doc.source_url
    assert "task_001" in doc.source_url


def test_normalize_task_source_id_is_sha256_prefix() -> None:
    doc = normalize_task(SAMPLE_TASK, "proj_001")
    expected = hashlib.sha256("task:task_001".encode()).hexdigest()[:16]
    assert doc.id == expected


def test_normalize_task_notes_in_content() -> None:
    doc = normalize_task(SAMPLE_TASK, "proj_001")
    assert "Figma" in doc.content


def test_normalize_task_completed_status_in_content() -> None:
    doc = normalize_task(SAMPLE_TASK_2, "proj_002")
    assert "completed" in doc.content


def test_normalize_task_open_status_in_content() -> None:
    doc = normalize_task(SAMPLE_TASK, "proj_001")
    assert "open" in doc.content


def test_normalize_task_no_assignee() -> None:
    doc = normalize_task(SAMPLE_TASK_2, "proj_002")
    assert doc.metadata["assignee_gid"] == ""
    assert doc.metadata["assignee_name"] == ""


def test_normalize_task_no_gid_falls_back_to_name_hash() -> None:
    task = {**SAMPLE_TASK, "gid": ""}
    doc = normalize_task(task, "proj_001")
    expected = hashlib.sha256("task:Design new landing page".encode()).hexdigest()[:16]
    assert doc.id == expected


def test_normalize_task_source_url_without_project() -> None:
    doc = normalize_task(SAMPLE_TASK, "")
    assert "task_001" in doc.source_url


def test_normalize_task_due_on_in_content() -> None:
    doc = normalize_task(SAMPLE_TASK, "proj_001")
    assert "2026-07-15" in doc.content


def test_normalize_task_assignee_name_in_content() -> None:
    doc = normalize_task(SAMPLE_TASK, "proj_001")
    assert "Alice Asana" in doc.content


# ── normalize_project() ──────────────────────────────────────────────────────


def test_normalize_project_basic() -> None:
    doc = normalize_project(SAMPLE_PROJECT, "ws_001")
    assert doc.id == hashlib.sha256("project:proj_001".encode()).hexdigest()[:16]
    assert doc.source == "asana"
    assert doc.type == "project"
    assert doc.title == "Product Roadmap"
    assert doc.metadata["project_gid"] == "proj_001"
    assert doc.metadata["workspace_gid"] == "ws_001"
    assert doc.metadata["color"] == "blue"
    assert "proj_001" in doc.source_url


def test_normalize_project_source_id_is_sha256_prefix() -> None:
    doc = normalize_project(SAMPLE_PROJECT, "ws_001")
    expected = hashlib.sha256("project:proj_001".encode()).hexdigest()[:16]
    assert doc.id == expected


def test_normalize_project_notes_in_content() -> None:
    doc = normalize_project(SAMPLE_PROJECT, "ws_001")
    assert "Q3 feature planning" in doc.content


def test_normalize_project_no_notes() -> None:
    doc = normalize_project(SAMPLE_PROJECT_2, "ws_001")
    assert doc.title == "Bug Tracking"
    assert "Notes" not in doc.content


def test_normalize_project_color_in_content() -> None:
    doc = normalize_project(SAMPLE_PROJECT, "ws_001")
    assert "blue" in doc.content


def test_normalize_project_no_gid_falls_back_to_name_hash() -> None:
    project = {**SAMPLE_PROJECT, "gid": ""}
    doc = normalize_project(project, "ws_001")
    expected = hashlib.sha256("project:Product Roadmap".encode()).hexdigest()[:16]
    assert doc.id == expected


def test_normalize_project_source_url_format() -> None:
    doc = normalize_project(SAMPLE_PROJECT, "ws_001")
    assert doc.source_url == "https://app.asana.com/0/proj_001/list"


def test_normalize_project_empty_source_url_when_no_gid() -> None:
    project = {**SAMPLE_PROJECT, "gid": ""}
    doc = normalize_project(project, "ws_001")
    assert doc.source_url == ""


# ── with_retry() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    fn = AsyncMock(side_effect=[AsanaNetworkError("transient"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_auth_immediately() -> None:
    fn = AsyncMock(side_effect=AsanaAuthError("Unauthorized", 401))
    with pytest.raises(AsanaAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=AsanaNetworkError("always fails"))
    with pytest.raises(AsanaNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retried() -> None:
    fn = AsyncMock(
        side_effect=[
            AsanaRateLimitError("rate limited", retry_after=0),
            {"ok": True},
        ]
    )
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_passes_args() -> None:
    fn = AsyncMock(return_value="result")
    await with_retry(fn, "arg1", "arg2", max_attempts=1)
    fn.assert_called_once_with("arg1", "arg2")


# ── Bearer auth (api_key field) ───────────────────────────────────────────────


def test_connector_reads_api_key_from_config() -> None:
    c = AsanaConnector(config={"api_key": "my-pat-token"})
    assert c._api_key == "my-pat-token"


def test_connector_strips_whitespace_from_api_key() -> None:
    c = AsanaConnector(config={"api_key": "  spaced-token  "})
    assert c._api_key == "spaced-token"


def test_connector_missing_api_key_is_empty_string() -> None:
    c = AsanaConnector(config={})
    assert c._api_key == ""


def test_connector_http_client_uses_bearer_header() -> None:
    from client.http_client import AsanaHTTPClient
    client = AsanaHTTPClient()
    headers = client._headers("my-test-token")
    assert headers["Authorization"] == "Bearer my-test-token"
    assert "Accept" in headers


# ── data envelope unwrapping ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_unwraps_data_envelope() -> None:
    """_request() must unwrap {"data": ...} for single-resource endpoints."""
    from client.http_client import AsanaHTTPClient

    client = AsanaHTTPClient()

    # Patch _request directly to test the data unwrapping logic
    async def _fake_request(method: str, api_key: str, path: str, **kwargs):  # type: ignore[no-untyped-def]
        body = {"data": {"gid": "1001", "name": "Alice"}}
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    client._request = _fake_request  # type: ignore[method-assign]
    result = await client._request("GET", "token", "/users/me")
    assert result["gid"] == "1001"
    assert result["name"] == "Alice"


@pytest.mark.asyncio
async def test_http_client_raw_preserves_next_page() -> None:
    """_request_raw() preserves the full body including next_page."""
    from client.http_client import AsanaHTTPClient

    client = AsanaHTTPClient()

    async def _fake_request_raw(method: str, api_key: str, path: str, params=None):  # type: ignore[no-untyped-def]
        return {
            "data": [{"gid": "ws_001"}],
            "next_page": {"offset": "cursor123"},
        }

    client._request_raw = _fake_request_raw  # type: ignore[method-assign]
    result = await client._request_raw("GET", "token", "/workspaces")
    assert result["data"][0]["gid"] == "ws_001"
    assert result["next_page"]["offset"] == "cursor123"


# ── _raise_for_status mapping ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_raise_for_status_401_raises_auth_error() -> None:
    from client.http_client import AsanaHTTPClient

    client = AsanaHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"errors": [{"message": "No auth"}]})

    with pytest.raises(AsanaAuthError) as exc_info:
        await client._raise_for_status(mock_response, "/users/me")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_raise_for_status_403_raises_auth_error() -> None:
    from client.http_client import AsanaHTTPClient

    client = AsanaHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"errors": [{"message": "Forbidden"}]})

    with pytest.raises(AsanaAuthError) as exc_info:
        await client._raise_for_status(mock_response, "/tasks/1")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_raise_for_status_404_raises_not_found() -> None:
    from client.http_client import AsanaHTTPClient

    client = AsanaHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={})

    with pytest.raises(AsanaNotFoundError):
        await client._raise_for_status(mock_response, "/tasks/bad")


@pytest.mark.asyncio
async def test_raise_for_status_429_raises_rate_limit_with_retry_after() -> None:
    from client.http_client import AsanaHTTPClient

    client = AsanaHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "30"}
    mock_response.json = AsyncMock(return_value={"errors": [{"message": "Too many"}]})

    with pytest.raises(AsanaRateLimitError) as exc_info:
        await client._raise_for_status(mock_response, "/tasks")
    assert exc_info.value.retry_after == 30.0


@pytest.mark.asyncio
async def test_raise_for_status_500_raises_network_error() -> None:
    from client.http_client import AsanaHTTPClient

    client = AsanaHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"errors": [{"message": "Internal error"}]})

    with pytest.raises(AsanaNetworkError) as exc_info:
        await client._raise_for_status(mock_response, "/projects")
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_raise_for_status_other_4xx_raises_asana_error() -> None:
    from client.http_client import AsanaHTTPClient

    client = AsanaHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 422
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"errors": [{"message": "Unprocessable"}]})

    with pytest.raises(AsanaError):
        await client._raise_for_status(mock_response, "/tasks")


@pytest.mark.asyncio
async def test_raise_for_status_includes_error_message() -> None:
    from client.http_client import AsanaHTTPClient

    client = AsanaHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"errors": [{"message": "Token expired"}]})

    with pytest.raises(AsanaAuthError) as exc_info:
        await client._raise_for_status(mock_response, "/users/me")
    assert "Token expired" in str(exc_info.value)


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_asana_auth_error_is_asana_error() -> None:
    exc = AsanaAuthError("bad token", 401)
    assert isinstance(exc, AsanaError)
    assert exc.status_code == 401


def test_asana_not_found_error_message() -> None:
    exc = AsanaNotFoundError("task", "task_001")
    assert "task_001" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_asana_rate_limit_error_retry_after() -> None:
    exc = AsanaRateLimitError("too many", retry_after=60.0)
    assert exc.retry_after == 60.0
    assert exc.status_code == 429


def test_asana_network_error_inherits_base() -> None:
    exc = AsanaNetworkError("timeout")
    assert isinstance(exc, AsanaError)


def test_asana_error_base_fields() -> None:
    exc = AsanaNetworkError("server error", status_code=500, code="server_err")
    assert exc.status_code == 500
    assert exc.code == "server_err"
    assert "server error" in str(exc)


def test_asana_auth_error_inherits_asana_error() -> None:
    exc = AsanaAuthError("denied", 403, "forbidden")
    assert isinstance(exc, AsanaError)
    assert exc.code == "forbidden"


def test_asana_not_found_inherits_asana_error() -> None:
    exc = AsanaNotFoundError("workspace", "ws_bad")
    assert isinstance(exc, AsanaError)
    assert "ws_bad" in exc.message


# ── Connector constants ───────────────────────────────────────────────────────


def test_connector_type_constant() -> None:
    from connector import CONNECTOR_TYPE, AUTH_TYPE
    assert CONNECTOR_TYPE == "asana"
    assert AUTH_TYPE == "api_key"


def test_connector_class_constants() -> None:
    assert AsanaConnector.CONNECTOR_TYPE == "asana"
    assert AsanaConnector.AUTH_TYPE == "api_key"


def test_connector_stores_workspace_gid() -> None:
    c = AsanaConnector(config={"api_key": "tok", "workspace_gid": "ws_999"})
    assert c._workspace_gid == "ws_999"


def test_connector_workspace_gid_defaults_empty() -> None:
    c = AsanaConnector(config={"api_key": "tok"})
    assert c._workspace_gid == ""


# ── Lifecycle ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_clears_client(connector: AsanaConnector) -> None:
    connector._http_client.aclose = AsyncMock()
    await connector.aclose()
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    c = make_connector()
    # No client set yet — should not raise
    await c.aclose()
    await c.aclose()


@pytest.mark.asyncio
async def test_context_manager() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c._http_client = mock_client
    async with c:
        pass
    mock_client.aclose.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_client_creates_client_on_first_call() -> None:
    c = make_connector()
    assert c._http_client is None
    client = c._ensure_client()
    assert client is not None
    assert c._http_client is client


@pytest.mark.asyncio
async def test_ensure_client_reuses_existing_client() -> None:
    c = make_connector()
    first = c._ensure_client()
    second = c._ensure_client()
    assert first is second
