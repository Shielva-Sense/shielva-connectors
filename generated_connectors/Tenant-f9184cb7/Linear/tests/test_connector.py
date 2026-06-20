"""Unit tests for LinearConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import LinearConnector
from exceptions import (
    LinearAuthError,
    LinearError,
    LinearNetworkError,
    LinearNotFoundError,
    LinearRateLimitError,
)
from helpers.utils import normalize_issue, normalize_project, with_retry, _short_hash
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ──────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_linear_test_001"
API_KEY = "lin_api_test_abc123"

SAMPLE_VIEWER_DATA: dict = {
    "viewer": {
        "name": "Alice Dev",
        "email": "alice@example.com",
    }
}

SAMPLE_TEAM: dict = {
    "id": "team_ENG001",
    "name": "Engineering",
    "key": "ENG",
}

SAMPLE_TEAMS_DATA: dict = {
    "teams": {
        "nodes": [SAMPLE_TEAM]
    }
}

SAMPLE_ISSUE: dict = {
    "id": "ISSUE_ABC001",
    "title": "Fix login bug",
    "description": "Users cannot log in after the last deploy.",
    "state": {"name": "In Progress"},
    "priority": 2,
    "assignee": {"name": "Bob Builder"},
    "team": {"name": "Engineering", "key": "ENG"},
    "createdAt": "2026-06-01T10:00:00Z",
    "updatedAt": "2026-06-02T12:00:00Z",
}

SAMPLE_ISSUE_2: dict = {
    "id": "ISSUE_ABC002",
    "title": "Add dark mode",
    "description": "Implement dark mode toggle.",
    "state": {"name": "Todo"},
    "priority": 3,
    "assignee": None,
    "team": {"name": "Engineering", "key": "ENG"},
    "createdAt": "2026-06-03T09:00:00Z",
    "updatedAt": "2026-06-04T11:00:00Z",
}

SAMPLE_ISSUES_PAGE: dict = {
    "issues": {
        "nodes": [SAMPLE_ISSUE],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
}

SAMPLE_ISSUES_PAGE_1: dict = {
    "issues": {
        "nodes": [SAMPLE_ISSUE],
        "pageInfo": {"hasNextPage": True, "endCursor": "cursor_abc"},
    }
}

SAMPLE_ISSUES_PAGE_2: dict = {
    "issues": {
        "nodes": [SAMPLE_ISSUE_2],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
}

SAMPLE_PROJECTS_DATA: dict = {
    "projects": {
        "nodes": [
            {
                "id": "proj_001",
                "name": "Platform Refresh",
                "description": "Major platform rewrite.",
                "state": "started",
            }
        ]
    }
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> LinearConnector:
    return LinearConnector(
        api_key=API_KEY,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: LinearConnector) -> LinearConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


# ── install() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success(connector: LinearConnector) -> None:
    instance = MagicMock()
    instance.graphql_query = AsyncMock(return_value=SAMPLE_VIEWER_DATA)
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Alice Dev" in result.message
    assert "alice@example.com" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = LinearConnector(tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector: LinearConnector) -> None:
    instance = MagicMock()
    instance.graphql_query = AsyncMock(
        side_effect=LinearAuthError("UNAUTHORIZED", code="AUTHENTICATION_ERROR")
    )
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "UNAUTHORIZED" in result.message


@pytest.mark.asyncio
async def test_install_network_error(connector: LinearConnector) -> None:
    instance = MagicMock()
    instance.graphql_query = AsyncMock(
        side_effect=LinearNetworkError("Connection refused")
    )
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_error(connector: LinearConnector) -> None:
    instance = MagicMock()
    instance.graphql_query = AsyncMock(side_effect=RuntimeError("unexpected"))
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_stores_client_on_success(connector: LinearConnector) -> None:
    instance = MagicMock()
    instance.graphql_query = AsyncMock(return_value=SAMPLE_VIEWER_DATA)
    connector._make_client = lambda: instance
    assert connector._http_client is None
    await connector.install()
    assert connector._http_client is not None


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: LinearConnector) -> None:
    instance = MagicMock()
    instance.graphql_query = AsyncMock(return_value=SAMPLE_VIEWER_DATA)
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Alice Dev" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: LinearConnector) -> None:
    instance = MagicMock()
    instance.graphql_query = AsyncMock(
        side_effect=LinearAuthError("Forbidden", code="FORBIDDEN")
    )
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: LinearConnector) -> None:
    instance = MagicMock()
    instance.graphql_query = AsyncMock(
        side_effect=LinearNetworkError("Timeout")
    )
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = LinearConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: LinearConnector) -> None:
    instance = MagicMock()
    instance.graphql_query = AsyncMock(side_effect=RuntimeError("boom"))
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty_teams(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(
        return_value={"teams": {"nodes": []}}
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_single_issue(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(
        side_effect=[
            SAMPLE_TEAMS_DATA,
            SAMPLE_ISSUES_PAGE,
        ]
    )
    result = await c.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_pagination(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(
        side_effect=[
            SAMPLE_TEAMS_DATA,
            SAMPLE_ISSUES_PAGE_1,
            SAMPLE_ISSUES_PAGE_2,
        ]
    )
    result = await c.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert c._http_client.graphql_query.call_count == 3  # teams + 2 issue pages


@pytest.mark.asyncio
async def test_sync_teams_api_error_returns_failed(
    connector_with_mock_client: LinearConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(
        side_effect=LinearError("server error", 500)
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_issues_api_error_returns_failed(
    connector_with_mock_client: LinearConnector,
) -> None:
    c = connector_with_mock_client
    # with_retry retries LinearError up to 3 times — provide enough side effects
    err = LinearError("issue fetch failed", 500)
    c._http_client.graphql_query = AsyncMock(
        side_effect=[
            SAMPLE_TEAMS_DATA,
            err, err, err,  # 3 retries before giving up
        ]
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "issue fetch failed" in result.message


@pytest.mark.asyncio
async def test_sync_partial_failure(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    bad_issue = {**SAMPLE_ISSUE, "id": None}  # will cause normalize_issue to produce odd source_id
    issues_page = {
        "issues": {
            "nodes": [SAMPLE_ISSUE, bad_issue],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    c._http_client.graphql_query = AsyncMock(
        side_effect=[SAMPLE_TEAMS_DATA, issues_page]
    )

    # Patch normalize_issue to fail on the second call
    call_count = {"n": 0}
    original_normalize = normalize_issue

    def patched_normalize(issue: dict, connector_id: str, tenant_id: str):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ValueError("normalize failed")
        return original_normalize(issue, connector_id, tenant_id)

    import helpers.utils as utils_mod
    original = utils_mod.normalize_issue
    utils_mod.normalize_issue = patched_normalize  # type: ignore[assignment]
    import connector as conn_mod
    original_conn = conn_mod.normalize_issue
    conn_mod.normalize_issue = patched_normalize  # type: ignore[assignment]
    try:
        result = await c.sync(full=True)
    finally:
        utils_mod.normalize_issue = original  # type: ignore[assignment]
        conn_mod.normalize_issue = original_conn  # type: ignore[assignment]

    assert result.status == SyncStatus.PARTIAL
    assert result.documents_found == 2
    assert result.documents_synced == 1
    assert result.documents_failed == 1


@pytest.mark.asyncio
async def test_sync_multiple_teams(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    two_teams_data = {
        "teams": {
            "nodes": [
                {"id": "team_A", "name": "Alpha", "key": "ALP"},
                {"id": "team_B", "name": "Beta", "key": "BET"},
            ]
        }
    }
    c._http_client.graphql_query = AsyncMock(
        side_effect=[
            two_teams_data,
            SAMPLE_ISSUES_PAGE,
            SAMPLE_ISSUES_PAGE,
        ]
    )
    result = await c.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2


# ── list_teams() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_teams_returns_nodes(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(return_value=SAMPLE_TEAMS_DATA)
    result = await c.list_teams()
    assert "nodes" in result
    assert result["nodes"][0]["id"] == "team_ENG001"


@pytest.mark.asyncio
async def test_list_teams_empty(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(return_value={"teams": {"nodes": []}})
    result = await c.list_teams()
    assert result["nodes"] == []


# ── list_issues() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_issues_no_filter(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(return_value=SAMPLE_ISSUES_PAGE)
    result = await c.list_issues()
    assert "nodes" in result
    assert result["nodes"][0]["id"] == "ISSUE_ABC001"


@pytest.mark.asyncio
async def test_list_issues_with_team_filter(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(return_value=SAMPLE_ISSUES_PAGE)
    await c.list_issues(team_id="team_ENG001", limit=25)
    call_args = c._http_client.graphql_query.call_args
    variables = call_args.args[2] if len(call_args.args) > 2 else call_args.kwargs.get("variables", {})
    assert variables["filter"]["team"]["id"]["eq"] == "team_ENG001"
    assert variables["first"] == 25


@pytest.mark.asyncio
async def test_list_issues_with_cursor(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(return_value=SAMPLE_ISSUES_PAGE_2)
    await c.list_issues(after="cursor_abc")
    call_args = c._http_client.graphql_query.call_args
    variables = call_args.args[2] if len(call_args.args) > 2 else call_args.kwargs.get("variables", {})
    assert variables["after"] == "cursor_abc"


@pytest.mark.asyncio
async def test_list_issues_default_limit(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(return_value=SAMPLE_ISSUES_PAGE)
    await c.list_issues()
    call_args = c._http_client.graphql_query.call_args
    variables = call_args.args[2] if len(call_args.args) > 2 else call_args.kwargs.get("variables", {})
    assert variables["first"] == 50


# ── get_issue() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_issue_success(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(
        return_value={"issue": SAMPLE_ISSUE}
    )
    result = await c.get_issue("ISSUE_ABC001")
    assert result["id"] == "ISSUE_ABC001"
    assert result["title"] == "Fix login bug"


@pytest.mark.asyncio
async def test_get_issue_not_found_raises(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(
        return_value={"issue": None}
    )
    with pytest.raises(LinearNotFoundError):
        await c.get_issue("ISSUE_MISSING")


@pytest.mark.asyncio
async def test_get_issue_passes_id(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(
        return_value={"issue": SAMPLE_ISSUE}
    )
    await c.get_issue("ISSUE_ABC001")
    call_args = c._http_client.graphql_query.call_args
    variables = call_args.args[2] if len(call_args.args) > 2 else call_args.kwargs.get("variables", {})
    assert variables["id"] == "ISSUE_ABC001"


# ── list_projects() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_projects_returns_nodes(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(return_value=SAMPLE_PROJECTS_DATA)
    result = await c.list_projects()
    assert "nodes" in result
    assert result["nodes"][0]["name"] == "Platform Refresh"


@pytest.mark.asyncio
async def test_list_projects_empty(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    c._http_client.graphql_query = AsyncMock(
        return_value={"projects": {"nodes": []}}
    )
    result = await c.list_projects()
    assert result["nodes"] == []


# ── normalize_issue() ─────────────────────────────────────────────────────────


def test_normalize_issue_basic() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "Fix login bug" in doc.title
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "linear.app" in doc.source_url


def test_normalize_issue_source_id_is_16_chars() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_issue_source_id_is_hex() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    int(doc.source_id, 16)  # raises ValueError if not hex


def test_normalize_issue_source_id_is_deterministic() -> None:
    doc1 = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_issue_different_ids_produce_different_source_ids() -> None:
    issue2 = {**SAMPLE_ISSUE, "id": "ISSUE_XYZ999"}
    doc1 = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_issue(issue2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_issue_metadata_fields() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["issue_id"] == "ISSUE_ABC001"
    assert meta["state"] == "In Progress"
    assert meta["priority"] == 2
    assert meta["priority_label"] == "High"
    assert meta["assignee"] == "Bob Builder"
    assert meta["team_name"] == "Engineering"
    assert meta["team_key"] == "ENG"
    assert meta["created_at"] == "2026-06-01T10:00:00Z"
    assert meta["updated_at"] == "2026-06-02T12:00:00Z"


def test_normalize_issue_content_includes_description() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "Users cannot log in after the last deploy." in doc.content


def test_normalize_issue_content_includes_state() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "In Progress" in doc.content


def test_normalize_issue_content_includes_priority() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "High" in doc.content


def test_normalize_issue_no_assignee() -> None:
    issue = {**SAMPLE_ISSUE, "assignee": None}
    doc = normalize_issue(issue, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["assignee"] == "Unassigned"


def test_normalize_issue_no_description() -> None:
    issue = {**SAMPLE_ISSUE, "description": None}
    doc = normalize_issue(issue, CONNECTOR_ID, TENANT_ID)
    assert doc.content is not None  # falls back to state/priority lines


def test_normalize_issue_priority_labels() -> None:
    for priority, label in [(0, "No Priority"), (1, "Urgent"), (2, "High"), (3, "Medium"), (4, "Low")]:
        issue = {**SAMPLE_ISSUE, "priority": priority}
        doc = normalize_issue(issue, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["priority_label"] == label


def test_normalize_issue_team_key_in_title() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "[ENG]" in doc.title


def test_normalize_issue_no_team() -> None:
    issue = {**SAMPLE_ISSUE, "team": None}
    doc = normalize_issue(issue, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["team_name"] == ""
    assert doc.metadata["team_key"] == ""
    assert doc.title == "Fix login bug"  # no team key prefix


# ── normalize_project() ───────────────────────────────────────────────────────


def test_normalize_project_basic() -> None:
    project = SAMPLE_PROJECTS_DATA["projects"]["nodes"][0]
    doc = normalize_project(project, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Platform Refresh"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "linear.app" in doc.source_url


def test_normalize_project_source_id_16_chars() -> None:
    project = SAMPLE_PROJECTS_DATA["projects"]["nodes"][0]
    doc = normalize_project(project, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_project_metadata() -> None:
    project = SAMPLE_PROJECTS_DATA["projects"]["nodes"][0]
    doc = normalize_project(project, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["project_id"] == "proj_001"
    assert doc.metadata["state"] == "started"


def test_normalize_project_no_description() -> None:
    project = {**SAMPLE_PROJECTS_DATA["projects"]["nodes"][0], "description": None}
    doc = normalize_project(project, CONNECTOR_ID, TENANT_ID)
    assert "started" in doc.content  # state line still present


# ── _short_hash() ─────────────────────────────────────────────────────────────


def test_short_hash_length() -> None:
    assert len(_short_hash("ISSUE_ABC001")) == 16


def test_short_hash_hex() -> None:
    h = _short_hash("ISSUE_ABC001")
    int(h, 16)  # raises if not hex


def test_short_hash_deterministic() -> None:
    assert _short_hash("test") == _short_hash("test")


def test_short_hash_different_inputs() -> None:
    assert _short_hash("ISSUE_A") != _short_hash("ISSUE_B")


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
        side_effect=[LinearNetworkError("fail"), LinearNetworkError("fail"), {"ok": True}]
    )
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=LinearAuthError("invalid creds"))
    with pytest.raises(LinearAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=LinearNetworkError("persistent failure"))
    with pytest.raises(LinearNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=LinearRateLimitError("429", retry_after=0))
    with pytest.raises(LinearRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_hierarchy_auth_is_linear_error() -> None:
    exc = LinearAuthError("bad creds")
    assert isinstance(exc, LinearError)


def test_exception_hierarchy_rate_limit_is_linear_error() -> None:
    exc = LinearRateLimitError("too fast")
    assert isinstance(exc, LinearError)
    assert exc.retry_after == 0.0


def test_exception_hierarchy_not_found_is_linear_error() -> None:
    exc = LinearNotFoundError("issue", "ISSUE_42")
    assert isinstance(exc, LinearError)
    assert exc.status_code == 404
    assert "ISSUE_42" in str(exc)


def test_exception_hierarchy_network_is_linear_error() -> None:
    exc = LinearNetworkError("timeout", 500)
    assert isinstance(exc, LinearError)


def test_rate_limit_stores_retry_after() -> None:
    exc = LinearRateLimitError("slow down", retry_after=60.0)
    assert exc.retry_after == 60.0


def test_linear_error_stores_status_code_and_code() -> None:
    exc = LinearError("problem", status_code=422, code="invalid")
    assert exc.status_code == 422
    assert exc.code == "invalid"


# ── HTTP client unit tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_passes_api_key_as_auth_header() -> None:
    from client.http_client import LinearHTTPClient
    client = LinearHTTPClient()
    headers = client._make_headers("lin_abc123")
    assert headers["Authorization"] == "lin_abc123"
    assert headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_http_client_graphql_error_raises_linear_auth_error() -> None:
    from client.http_client import LinearHTTPClient
    client = LinearHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(
        return_value={
            "errors": [
                {
                    "message": "authentication required",
                    "extensions": {"code": "AUTHENTICATION_ERROR"},
                }
            ]
        }
    )
    with pytest.raises(LinearAuthError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_http_client_graphql_error_raises_linear_not_found() -> None:
    from client.http_client import LinearHTTPClient
    client = LinearHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(
        return_value={
            "errors": [
                {
                    "message": "Entity not found",
                    "extensions": {"code": "NOT_FOUND"},
                }
            ]
        }
    )
    with pytest.raises(LinearNotFoundError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_http_client_429_raises_rate_limit_error() -> None:
    from client.http_client import LinearHTTPClient
    client = LinearHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "30"}
    with pytest.raises(LinearRateLimitError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.retry_after == 30.0


@pytest.mark.asyncio
async def test_http_client_401_raises_auth_error() -> None:
    from client.http_client import LinearHTTPClient
    client = LinearHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 401
    with pytest.raises(LinearAuthError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_http_client_500_raises_network_error() -> None:
    from client.http_client import LinearHTTPClient
    client = LinearHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 500
    with pytest.raises(LinearNetworkError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_http_client_missing_data_field_raises() -> None:
    from client.http_client import LinearHTTPClient
    client = LinearHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"errors": [], "data": None})
    with pytest.raises(LinearError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_http_client_success_returns_data() -> None:
    from client.http_client import LinearHTTPClient
    client = LinearHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(
        return_value={"data": {"viewer": {"name": "Alice"}}}
    )
    result = await client._handle_response(mock_response)
    assert result == {"viewer": {"name": "Alice"}}


# ── Connector config loading ──────────────────────────────────────────────────


def test_connector_loads_from_config_dict() -> None:
    c = LinearConnector(config={"api_key": "lin_from_config"})
    assert c._api_key == "lin_from_config"


def test_connector_kwarg_api_key() -> None:
    c = LinearConnector(api_key="lin_from_kwarg")
    assert c._api_key == "lin_from_kwarg"


def test_connector_config_takes_precedence_over_kwarg() -> None:
    c = LinearConnector(
        config={"api_key": "lin_config_key"},
        api_key="lin_kwarg_key",
    )
    assert c._api_key == "lin_config_key"


def test_connector_missing_credentials_list() -> None:
    c = LinearConnector()
    missing = c._missing_credentials()
    assert "api_key" in missing


def test_connector_no_missing_when_api_key_set() -> None:
    c = LinearConnector(api_key="lin_abc")
    missing = c._missing_credentials()
    assert missing == []


# ── Connector lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_context_manager(connector: LinearConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(connector_with_mock_client: LinearConnector) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


def test_ensure_client_creates_on_first_call(connector: LinearConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: LinearConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2


def test_connector_type_constants() -> None:
    assert LinearConnector.CONNECTOR_TYPE == "linear"
    assert LinearConnector.AUTH_TYPE == "api_key"
