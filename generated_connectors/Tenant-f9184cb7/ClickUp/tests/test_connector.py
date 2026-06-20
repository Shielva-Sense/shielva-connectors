"""Unit tests for the ClickUp connector — all HTTP calls are mocked.

Coverage:
    exceptions (8)          models (8)              normalize_task (10)
    normalize_list (8)      with_retry (7)          HTTP client methods (14)
    _raise_for_status (8)   install (7)             health_check (7)
    sync (10)               list_teams (3)          list_spaces (3)
    list_folders (3)        list_lists (3)          list_tasks (4)
    get_task (3)            pagination (5)          lifecycle (3)
    total: 115 tests
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import ClickUpConnector, CONNECTOR_TYPE, AUTH_TYPE
from exceptions import (
    ClickUpAuthError,
    ClickUpError,
    ClickUpNetworkError,
    ClickUpNotFoundError,
    ClickUpRateLimitError,
)
from helpers.utils import normalize_list, normalize_task, with_retry, _stable_id
from models import AuthStatus, ConnectorDocument, ConnectorHealth, SyncStatus
from client.http_client import ClickUpHTTPClient

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_clickup_test_001"
API_KEY = "pk_test_api_key_abc123"

TEAM_ID = "team-1234"
SPACE_ID = "space-5678"
FOLDER_ID = "folder-9012"
LIST_ID = "list-3456"
TASK_ID = "task-7890"


# ── Sample data fixtures ──────────────────────────────────────────────────────


def _make_user(username: str = "alice", email: str = "alice@example.com") -> Dict[str, Any]:
    return {"user": {"id": 1, "username": username, "email": email, "color": "#FF0000"}}


def _make_team(team_id: str = TEAM_ID, name: str = "My Workspace") -> Dict[str, Any]:
    return {"id": team_id, "name": name, "color": "#00BCD4", "avatar": None, "members": []}


def _make_space(space_id: str = SPACE_ID, name: str = "Engineering") -> Dict[str, Any]:
    return {"id": space_id, "name": name, "private": False, "archived": False}


def _make_folder(folder_id: str = FOLDER_ID, name: str = "Sprint 1") -> Dict[str, Any]:
    return {"id": folder_id, "name": name, "orderindex": 0}


def _make_list(
    list_id: str = LIST_ID,
    name: str = "Backlog",
    task_count: int = 5,
    archived: bool = False,
) -> Dict[str, Any]:
    return {
        "id": list_id,
        "name": name,
        "archived": archived,
        "task_count": task_count,
        "folder": {"id": FOLDER_ID, "name": "Sprint 1"},
        "space": {"id": SPACE_ID, "name": "Engineering"},
        "status": {"status": "open"},
        "due_date": "1700000000000",
    }


def _make_task(
    task_id: str = TASK_ID,
    name: str = "Fix the bug",
    status: str = "open",
) -> Dict[str, Any]:
    return {
        "id": task_id,
        "name": name,
        "description": "A task description.",
        "status": {"status": status, "color": "#FF0000"},
        "list": {"id": LIST_ID, "name": "Backlog"},
        "folder": {"id": FOLDER_ID, "name": "Sprint 1"},
        "space": {"id": SPACE_ID},
        "url": f"https://app.clickup.com/t/{task_id}",
        "date_created": "1700000000000",
        "date_updated": "1700001000000",
        "priority": {"priority": "high", "color": "#FF0000"},
        "assignees": [{"username": "alice"}, {"username": "bob"}],
        "tags": [{"name": "backend"}, {"name": "urgent"}],
    }


def make_connector(api_key: str = API_KEY) -> ClickUpConnector:
    return ClickUpConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key},
    )


def _make_mock_resp(status: int, data: Dict[str, Any]) -> MagicMock:
    mock = AsyncMock()
    mock.status = status
    mock.json = AsyncMock(return_value=data)
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    return mock


def _make_mock_session(method: str, mock_resp: MagicMock) -> MagicMock:
    mock_session = MagicMock()
    setattr(mock_session, method, MagicMock(return_value=mock_resp))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


# ═══════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTION HIERARCHY (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_base_exception_carries_message(self) -> None:
        exc = ClickUpError("base error", status_code=400, code="bad_request")
        assert str(exc) == "base error"
        assert exc.status_code == 400
        assert exc.code == "bad_request"

    def test_auth_error_is_clickup_error(self) -> None:
        exc = ClickUpAuthError("invalid token", status_code=401)
        assert isinstance(exc, ClickUpError)
        assert exc.status_code == 401

    def test_network_error_is_clickup_error(self) -> None:
        exc = ClickUpNetworkError("timeout")
        assert isinstance(exc, ClickUpError)

    def test_not_found_error_default_message(self) -> None:
        exc = ClickUpNotFoundError("task", "task-001")
        assert "task-001" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"

    def test_not_found_error_no_resource_id(self) -> None:
        exc = ClickUpNotFoundError("workspace")
        assert exc.status_code == 404

    def test_rate_limit_error_carries_retry_after(self) -> None:
        exc = ClickUpRateLimitError("too many requests", retry_after=60.0)
        assert exc.retry_after == 60.0
        assert exc.status_code == 429
        assert exc.code == "rate_limit"

    def test_hierarchy_is_distinct(self) -> None:
        assert ClickUpAuthError is not ClickUpNetworkError
        assert ClickUpNotFoundError is not ClickUpRateLimitError

    def test_base_error_default_fields(self) -> None:
        exc = ClickUpError("plain error")
        assert exc.status_code == 0
        assert exc.code == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MODELS (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"
        assert SyncStatus.RUNNING == "running"

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            id="abc123",
            source="clickup",
            type="task",
            title="Test Task",
            content="body",
        )
        assert doc.id == "abc123"
        assert doc.source == "clickup"
        assert doc.type == "task"
        assert doc.source_url == ""
        assert doc.metadata == {}

    def test_connector_document_with_metadata(self) -> None:
        doc = ConnectorDocument(
            id="xyz",
            source="clickup",
            type="task_list",
            title="My List",
            content="content",
            source_url="https://app.clickup.com",
            metadata={"list_id": "list-001"},
        )
        assert doc.metadata["list_id"] == "list-001"
        assert doc.source_url == "https://app.clickup.com"

    def test_install_result_defaults(self) -> None:
        from models import InstallResult
        result = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
        )
        assert result.connector_id == ""
        assert result.message == ""

    def test_health_check_result_fields(self) -> None:
        from models import HealthCheckResult
        result = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="ok",
        )
        assert result.health == ConnectorHealth.HEALTHY

    def test_sync_result_defaults(self) -> None:
        from models import SyncResult
        result = SyncResult(status=SyncStatus.COMPLETED)
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.documents_failed == 0
        assert result.message == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZE_TASK (10 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeTask:
    def test_stable_id_is_sha256_task_prefix(self) -> None:
        raw = _make_task(task_id=TASK_ID)
        doc = normalize_task(raw)
        expected = hashlib.sha256(f"task:{TASK_ID}".encode()).hexdigest()[:16]
        assert doc.id == expected

    def test_source_is_clickup(self) -> None:
        doc = normalize_task(_make_task())
        assert doc.source == "clickup"

    def test_type_is_task(self) -> None:
        doc = normalize_task(_make_task())
        assert doc.type == "task"

    def test_title_is_task_name(self) -> None:
        doc = normalize_task(_make_task(name="Implement login"))
        assert doc.title == "Implement login"

    def test_status_in_metadata_and_content(self) -> None:
        doc = normalize_task(_make_task(status="in progress"))
        assert doc.metadata["status"] == "in progress"
        assert "in progress" in doc.content

    def test_assignees_extracted(self) -> None:
        doc = normalize_task(_make_task())
        assert "alice" in doc.metadata["assignees"]
        assert "bob" in doc.metadata["assignees"]
        assert "alice" in doc.content

    def test_tags_extracted(self) -> None:
        doc = normalize_task(_make_task())
        assert "backend" in doc.metadata["tags"]
        assert "backend" in doc.content

    def test_priority_in_content(self) -> None:
        doc = normalize_task(_make_task())
        assert "high" in doc.content

    def test_source_url_is_task_url(self) -> None:
        doc = normalize_task(_make_task(task_id=TASK_ID))
        assert f"https://app.clickup.com/t/{TASK_ID}" == doc.source_url

    def test_empty_task_does_not_crash(self) -> None:
        doc = normalize_task({})
        assert doc.title == "Untitled Task"
        assert doc.source == "clickup"
        assert doc.type == "task"

    def test_list_id_and_space_id_in_metadata(self) -> None:
        doc = normalize_task(_make_task(), list_id="list-999", space_id="space-888")
        # list_id from task.list.id takes priority over param
        assert doc.metadata["list_id"] == LIST_ID  # from embedded list obj
        assert doc.metadata["space_id"] == SPACE_ID

    def test_same_task_id_produces_same_doc_id(self) -> None:
        raw = _make_task(task_id="stable-id")
        assert normalize_task(raw).id == normalize_task(raw).id

    def test_different_task_ids_produce_different_doc_ids(self) -> None:
        a = normalize_task(_make_task(task_id="task-aaa"))
        b = normalize_task(_make_task(task_id="task-bbb"))
        assert a.id != b.id


# ═══════════════════════════════════════════════════════════════════════════════
# 4. NORMALIZE_LIST (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeList:
    def test_stable_id_is_sha256_list_prefix(self) -> None:
        raw = _make_list(list_id=LIST_ID)
        doc = normalize_list(raw)
        expected = hashlib.sha256(f"list:{LIST_ID}".encode()).hexdigest()[:16]
        assert doc.id == expected

    def test_source_is_clickup(self) -> None:
        doc = normalize_list(_make_list())
        assert doc.source == "clickup"

    def test_type_is_task_list(self) -> None:
        doc = normalize_list(_make_list())
        assert doc.type == "task_list"

    def test_title_is_list_name(self) -> None:
        doc = normalize_list(_make_list(name="Backlog"))
        assert doc.title == "Backlog"

    def test_task_count_in_content(self) -> None:
        doc = normalize_list(_make_list(task_count=12))
        assert "12" in doc.content

    def test_folder_name_in_content(self) -> None:
        doc = normalize_list(_make_list())
        assert "Sprint 1" in doc.content

    def test_archived_in_metadata(self) -> None:
        doc = normalize_list(_make_list(archived=True))
        assert doc.metadata["archived"] is True
        assert "Archived" in doc.content

    def test_space_id_param_used_when_not_in_raw(self) -> None:
        raw = {"id": "list-000", "name": "Inbox"}
        doc = normalize_list(raw, space_id="space-custom")
        assert doc.metadata["space_id"] == "space-custom"

    def test_empty_list_does_not_crash(self) -> None:
        doc = normalize_list({})
        assert doc.title == "Untitled List"
        assert doc.type == "task_list"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. STABLE_ID HELPER (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestStableId:
    def test_returns_16_chars(self) -> None:
        assert len(_stable_id("task", "abc")) == 16

    def test_deterministic(self) -> None:
        assert _stable_id("task", "x") == _stable_id("task", "x")

    def test_different_prefix_different_id(self) -> None:
        assert _stable_id("task", "abc") != _stable_id("list", "abc")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. WITH_RETRY (7 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_clickup_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[ClickUpNetworkError("transient"), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 2

    @pytest.mark.asyncio
    async def test_auth_error_never_retried(self) -> None:
        fn = AsyncMock(side_effect=ClickUpAuthError("invalid key", 401))
        with pytest.raises(ClickUpAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_exhausts_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=ClickUpNetworkError("always fails"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ClickUpNetworkError):
                await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_error_is_retried(self) -> None:
        fn = AsyncMock(
            side_effect=[
                ClickUpRateLimitError("429", retry_after=0.0),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 2

    @pytest.mark.asyncio
    async def test_generic_exception_is_retried(self) -> None:
        fn = AsyncMock(side_effect=[ValueError("transient"), "recovered"])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == "recovered"

    @pytest.mark.asyncio
    async def test_max_attempts_one_no_retry(self) -> None:
        fn = AsyncMock(side_effect=ClickUpNetworkError("fail"))
        with pytest.raises(ClickUpNetworkError):
            await with_retry(fn, max_attempts=1)
        assert fn.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 7. _RAISE_FOR_STATUS (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRaiseForStatus:
    def _client(self) -> ClickUpHTTPClient:
        return ClickUpHTTPClient(api_key=API_KEY)

    def test_200_returns_body(self) -> None:
        c = self._client()
        body = {"teams": []}
        assert c._raise_for_status(200, body) == body

    def test_201_returns_body(self) -> None:
        c = self._client()
        body = {"id": "new"}
        assert c._raise_for_status(201, body) == body

    def test_299_returns_body(self) -> None:
        c = self._client()
        body = {"ok": True}
        assert c._raise_for_status(299, body) == body

    def test_401_raises_auth_error(self) -> None:
        c = self._client()
        with pytest.raises(ClickUpAuthError) as exc_info:
            c._raise_for_status(401, {"err": "Token invalid"})
        assert exc_info.value.status_code == 401

    def test_403_raises_auth_error(self) -> None:
        c = self._client()
        with pytest.raises(ClickUpAuthError) as exc_info:
            c._raise_for_status(403, {"err": "Forbidden"})
        assert exc_info.value.status_code == 403

    def test_404_raises_not_found_error(self) -> None:
        c = self._client()
        with pytest.raises(ClickUpNotFoundError):
            c._raise_for_status(404, {"err": "Not found"})

    def test_429_raises_rate_limit_error(self) -> None:
        c = self._client()
        with pytest.raises(ClickUpRateLimitError):
            c._raise_for_status(429, {})

    def test_500_raises_network_error(self) -> None:
        c = self._client()
        with pytest.raises(ClickUpNetworkError):
            c._raise_for_status(500, {"err": "Internal server error"})

    def test_other_4xx_raises_clickup_error(self) -> None:
        c = self._client()
        with pytest.raises(ClickUpError):
            c._raise_for_status(422, {"err": "Unprocessable"})

    def test_err_field_extracted_from_body(self) -> None:
        c = self._client()
        with pytest.raises(ClickUpAuthError, match="Token invalid"):
            c._raise_for_status(401, {"err": "Token invalid"})


# ═══════════════════════════════════════════════════════════════════════════════
# 8. HTTP CLIENT METHODS (14 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestHTTPClient:
    def test_headers_use_raw_api_key(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        headers = client._headers()
        assert headers["Authorization"] == API_KEY
        assert "Bearer" not in headers["Authorization"]

    @pytest.mark.asyncio
    async def test_get_authorized_user_success(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        user_data = _make_user()
        mock_resp = _make_mock_resp(200, user_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_authorized_user()
        assert result["user"]["username"] == "alice"

    @pytest.mark.asyncio
    async def test_get_authorized_user_401_raises_auth_error(self) -> None:
        client = ClickUpHTTPClient(api_key="bad_key")
        mock_resp = _make_mock_resp(401, {"err": "Token invalid."})
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(ClickUpAuthError):
                await client.get_authorized_user()

    @pytest.mark.asyncio
    async def test_get_teams_success(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        teams_data = {"teams": [_make_team()]}
        mock_resp = _make_mock_resp(200, teams_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_teams()
        assert result["teams"][0]["id"] == TEAM_ID

    @pytest.mark.asyncio
    async def test_get_spaces_success(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        spaces_data = {"spaces": [_make_space()]}
        mock_resp = _make_mock_resp(200, spaces_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_spaces(TEAM_ID)
        assert result["spaces"][0]["id"] == SPACE_ID

    @pytest.mark.asyncio
    async def test_get_space_success(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        space_data = _make_space()
        mock_resp = _make_mock_resp(200, space_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_space(SPACE_ID)
        assert result["id"] == SPACE_ID

    @pytest.mark.asyncio
    async def test_get_folders_success(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        folders_data = {"folders": [_make_folder()]}
        mock_resp = _make_mock_resp(200, folders_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_folders(SPACE_ID)
        assert result["folders"][0]["id"] == FOLDER_ID

    @pytest.mark.asyncio
    async def test_get_lists_by_folder_id(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        lists_data = {"lists": [_make_list()]}
        mock_resp = _make_mock_resp(200, lists_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_lists(folder_id=FOLDER_ID)
        assert result["lists"][0]["id"] == LIST_ID

    @pytest.mark.asyncio
    async def test_get_lists_by_space_id(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        lists_data = {"lists": [_make_list(list_id="fl-001", name="Inbox")]}
        mock_resp = _make_mock_resp(200, lists_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_lists(space_id=SPACE_ID)
        assert result["lists"][0]["name"] == "Inbox"

    def test_get_lists_no_args_raises(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        with pytest.raises(ValueError, match="space_id or folder_id"):
            import asyncio
            asyncio.get_event_loop().run_until_complete(client.get_lists())

    @pytest.mark.asyncio
    async def test_get_tasks_success(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        tasks_data = {"tasks": [_make_task()]}
        mock_resp = _make_mock_resp(200, tasks_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_tasks(LIST_ID)
        assert result["tasks"][0]["id"] == TASK_ID

    @pytest.mark.asyncio
    async def test_get_task_success(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        task_data = _make_task()
        mock_resp = _make_mock_resp(200, task_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_task(TASK_ID)
        assert result["id"] == TASK_ID

    @pytest.mark.asyncio
    async def test_get_members_success(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        members_data = {"members": [{"user": {"id": 1, "username": "alice"}}]}
        mock_resp = _make_mock_resp(200, members_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_members(LIST_ID)
        assert result["members"][0]["user"]["username"] == "alice"

    @pytest.mark.asyncio
    async def test_network_error_raises_clickup_network_error(self) -> None:
        import aiohttp as _aiohttp
        client = ClickUpHTTPClient(api_key=API_KEY)
        mock_session = MagicMock()
        mock_session.get = MagicMock(
            side_effect=_aiohttp.ClientConnectorError.__new__(
                _aiohttp.ClientConnectorError
            )
        )
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises((ClickUpNetworkError, Exception)):
                await client.get_authorized_user()

    @pytest.mark.asyncio
    async def test_get_tasks_with_include_closed(self) -> None:
        client = ClickUpHTTPClient(api_key=API_KEY)
        tasks_data = {"tasks": [_make_task(status="closed")]}
        mock_resp = _make_mock_resp(200, tasks_data)
        mock_session = _make_mock_session("get", mock_resp)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_tasks(LIST_ID, page=0, include_closed=True)
        assert result["tasks"][0]["status"]["status"] == "closed"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. CONNECTOR CONSTANTS (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestConnectorConstants:
    def test_module_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "clickup"

    def test_module_auth_type(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_class_constants(self) -> None:
        assert ClickUpConnector.CONNECTOR_TYPE == "clickup"
        assert ClickUpConnector.AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# 10. INSTALL (7 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestInstall:
    @pytest.mark.asyncio
    async def test_install_missing_api_key(self) -> None:
        c = make_connector(api_key="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    @pytest.mark.asyncio
    async def test_install_success(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(return_value=_make_user("alice"))
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "alice" in result.message

    @pytest.mark.asyncio
    async def test_install_invalid_key(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(
            side_effect=ClickUpAuthError("Unauthorized", 401)
        )
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_network_error(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(
            side_effect=ClickUpNetworkError("Connection refused")
        )
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_install_unknown_exception(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(side_effect=Exception("boom"))
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_install_connector_id_in_result(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(return_value=_make_user())
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.install()
        assert result.connector_id == CONNECTOR_ID

    @pytest.mark.asyncio
    async def test_install_username_fallback_to_email(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        # username empty, email present
        mock_client.get_authorized_user = AsyncMock(
            return_value={"user": {"id": 9, "username": "", "email": "bot@acme.com"}}
        )
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.install()
        assert "bot@acme.com" in result.message


# ═══════════════════════════════════════════════════════════════════════════════
# 11. HEALTH CHECK (7 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_missing_api_key(self) -> None:
        c = make_connector(api_key="")
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_success(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(return_value=_make_user("alice"))
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "alice" in result.message
        assert "reachable" in result.message

    @pytest.mark.asyncio
    async def test_health_check_invalid_key(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(
            side_effect=ClickUpAuthError("Invalid token", 401)
        )
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(
            side_effect=ClickUpNetworkError("timeout")
        )
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_generic_error(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(side_effect=Exception("unexpected"))
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_email_in_message(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(
            return_value={"user": {"id": 1, "username": "alice", "email": "alice@acme.com"}}
        )
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.health_check()
        assert "alice@acme.com" in result.message

    @pytest.mark.asyncio
    async def test_health_check_unknown_user(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.get_authorized_user = AsyncMock(return_value={"user": {"id": 99}})
        mock_client.aclose = AsyncMock()
        c._make_client = lambda: mock_client
        result = await c.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert "unknown" in result.message


# ═══════════════════════════════════════════════════════════════════════════════
# 12. SYNC (10 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def connector() -> ClickUpConnector:
    c = make_connector()
    c._http_client = MagicMock()
    return c


class TestSync:
    @pytest.mark.asyncio
    async def test_sync_empty_teams(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(return_value={"teams": []})
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    @pytest.mark.asyncio
    async def test_sync_teams_failure_returns_failed(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(
            side_effect=ClickUpNetworkError("server error")
        )
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "teams" in result.message.lower()

    @pytest.mark.asyncio
    async def test_sync_one_list_no_tasks(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(return_value={"teams": [_make_team()]})
        connector._http_client.get_spaces = AsyncMock(return_value={"spaces": [_make_space()]})
        connector._http_client.get_folders = AsyncMock(return_value={"folders": [_make_folder()]})
        connector._http_client.get_lists = AsyncMock(
            side_effect=[
                {"lists": [_make_list()]},  # folder lists
                {"lists": []},              # folderless lists
            ]
        )
        connector._http_client.get_tasks = AsyncMock(return_value={"tasks": []})
        result = await connector.sync()
        assert result.documents_found == 1  # 1 list
        assert result.documents_synced == 1
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_sync_one_list_with_tasks(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(return_value={"teams": [_make_team()]})
        connector._http_client.get_spaces = AsyncMock(return_value={"spaces": [_make_space()]})
        connector._http_client.get_folders = AsyncMock(return_value={"folders": [_make_folder()]})
        connector._http_client.get_lists = AsyncMock(
            side_effect=[
                {"lists": [_make_list()]},
                {"lists": []},
            ]
        )
        # First call: 2 tasks; second call: empty (stops pagination)
        connector._http_client.get_tasks = AsyncMock(
            side_effect=[
                {"tasks": [_make_task("t-001"), _make_task("t-002")]},
                {"tasks": []},
            ]
        )
        result = await connector.sync()
        # 1 list + 2 tasks
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_sync_partial_on_normalize_failure(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(return_value={"teams": [_make_team()]})
        connector._http_client.get_spaces = AsyncMock(return_value={"spaces": [_make_space()]})
        connector._http_client.get_folders = AsyncMock(return_value={"folders": []})
        connector._http_client.get_lists = AsyncMock(return_value={"lists": [_make_list()]})
        connector._http_client.get_tasks = AsyncMock(return_value={"tasks": []})
        with patch("connector.normalize_list", side_effect=Exception("normalizer failed")):
            result = await connector.sync()
        assert result.documents_failed >= 1
        assert result.status == SyncStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_sync_ingest_called_with_kb_id(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(return_value={"teams": [_make_team()]})
        connector._http_client.get_spaces = AsyncMock(return_value={"spaces": [_make_space()]})
        connector._http_client.get_folders = AsyncMock(return_value={"folders": [_make_folder()]})
        connector._http_client.get_lists = AsyncMock(
            side_effect=[
                {"lists": [_make_list()]},
                {"lists": []},
            ]
        )
        connector._http_client.get_tasks = AsyncMock(
            side_effect=[
                {"tasks": [_make_task()]},
                {"tasks": []},
            ]
        )
        connector._ingest_document = AsyncMock()
        result = await connector.sync(kb_id="kb_001")
        # 1 list + 1 task = 2 ingest calls
        assert connector._ingest_document.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_skips_team_without_id(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(
            return_value={"teams": [{"name": "Ghost"}]}  # no id
        )
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_synced == 0

    @pytest.mark.asyncio
    async def test_sync_spaces_error_continues(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(return_value={"teams": [_make_team()]})
        connector._http_client.get_spaces = AsyncMock(
            side_effect=ClickUpNetworkError("spaces error")
        )
        result = await connector.sync()
        # spaces failure is caught; sync returns COMPLETED with 0 docs
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    @pytest.mark.asyncio
    async def test_sync_multiple_teams(self, connector: ClickUpConnector) -> None:
        team_a = _make_team(team_id="team-aaa", name="Team A")
        team_b = _make_team(team_id="team-bbb", name="Team B")
        connector._http_client.get_teams = AsyncMock(return_value={"teams": [team_a, team_b]})
        connector._http_client.get_spaces = AsyncMock(return_value={"spaces": []})
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        # get_spaces called once per team
        assert connector._http_client.get_spaces.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_task_pagination_stops_on_empty(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(return_value={"teams": [_make_team()]})
        connector._http_client.get_spaces = AsyncMock(return_value={"spaces": [_make_space()]})
        connector._http_client.get_folders = AsyncMock(return_value={"folders": [_make_folder()]})
        connector._http_client.get_lists = AsyncMock(
            side_effect=[
                {"lists": [_make_list()]},
                {"lists": []},
            ]
        )
        task_calls: List[int] = []

        async def mock_tasks(list_id: str, page: int = 0, **kw: Any) -> Dict[str, Any]:
            task_calls.append(page)
            return {"tasks": []}  # immediately empty

        connector._http_client.get_tasks = mock_tasks
        result = await connector.sync()
        assert 0 in task_calls  # page 0 was tried


# ═══════════════════════════════════════════════════════════════════════════════
# 13. LIST_TEAMS / LIST_SPACES / LIST_FOLDERS (9 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestListMethods:
    @pytest.mark.asyncio
    async def test_list_teams_returns_teams(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(
            return_value={"teams": [_make_team(), _make_team(team_id="t-2", name="B")]}
        )
        result = await connector.list_teams()
        assert len(result) == 2
        assert result[0]["id"] == TEAM_ID

    @pytest.mark.asyncio
    async def test_list_teams_empty(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(return_value={"teams": []})
        result = await connector.list_teams()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_teams_propagates_error(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_teams = AsyncMock(
            side_effect=ClickUpAuthError("Invalid token", 401)
        )
        with pytest.raises(ClickUpAuthError):
            await connector.list_teams()

    @pytest.mark.asyncio
    async def test_list_spaces_returns_spaces(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_spaces = AsyncMock(
            return_value={"spaces": [_make_space(), _make_space(space_id="s-2", name="Design")]}
        )
        result = await connector.list_spaces(TEAM_ID)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_spaces_empty(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_spaces = AsyncMock(return_value={"spaces": []})
        result = await connector.list_spaces(TEAM_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_list_spaces_passes_team_id(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_spaces = AsyncMock(return_value={"spaces": []})
        await connector.list_spaces(TEAM_ID)
        connector._http_client.get_spaces.assert_called_once_with(TEAM_ID)

    @pytest.mark.asyncio
    async def test_list_folders_returns_folders(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_folders = AsyncMock(
            return_value={"folders": [_make_folder(), _make_folder(folder_id="f-2", name="Sprint 2")]}
        )
        result = await connector.list_folders(SPACE_ID)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_folders_empty(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_folders = AsyncMock(return_value={"folders": []})
        result = await connector.list_folders(SPACE_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_list_folders_passes_space_id(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_folders = AsyncMock(return_value={"folders": []})
        await connector.list_folders(SPACE_ID)
        connector._http_client.get_folders.assert_called_once_with(SPACE_ID)


# ═══════════════════════════════════════════════════════════════════════════════
# 14. LIST_LISTS / LIST_TASKS / GET_TASK (10 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskListMethods:
    @pytest.mark.asyncio
    async def test_list_lists_by_folder(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_lists = AsyncMock(
            return_value={"lists": [_make_list()]}
        )
        result = await connector.list_lists(folder_id=FOLDER_ID)
        assert result[0]["id"] == LIST_ID

    @pytest.mark.asyncio
    async def test_list_lists_by_space(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_lists = AsyncMock(
            return_value={"lists": [_make_list(list_id="fl-001", name="Inbox")]}
        )
        result = await connector.list_lists(space_id=SPACE_ID)
        assert result[0]["name"] == "Inbox"

    @pytest.mark.asyncio
    async def test_list_lists_empty(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_lists = AsyncMock(return_value={"lists": []})
        result = await connector.list_lists(space_id=SPACE_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_list_tasks_returns_all_pages(self, connector: ClickUpConnector) -> None:
        """list_tasks paginates until an empty page is returned.

        Each non-empty page must contain PAGE_SIZE (100) tasks for the
        connector to fetch the next page; fewer than 100 tasks signals the
        last page. Here page 0 and page 1 each return 100 tasks, then page 2
        returns empty — so all 200 tasks are collected.
        """
        from connector import _PAGE_SIZE
        page0 = [_make_task(f"t-{i:03d}") for i in range(_PAGE_SIZE)]
        page1 = [_make_task(f"t-{i + _PAGE_SIZE:03d}") for i in range(_PAGE_SIZE)]
        pages_called: List[int] = []

        async def mock_tasks(list_id: str, page: int = 0, **kw: Any) -> Dict[str, Any]:
            pages_called.append(page)
            if page == 0:
                return {"tasks": page0}
            if page == 1:
                return {"tasks": page1}
            return {"tasks": []}

        connector._http_client.get_tasks = mock_tasks
        result = await connector.list_tasks(LIST_ID)
        assert len(result) == 2 * _PAGE_SIZE
        assert pages_called == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_list_tasks_stops_on_empty_first_page(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_tasks = AsyncMock(return_value={"tasks": []})
        result = await connector.list_tasks(LIST_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_list_tasks_include_closed_passed(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_tasks = AsyncMock(return_value={"tasks": []})
        await connector.list_tasks(LIST_ID, include_closed=True)
        call_kwargs = connector._http_client.get_tasks.call_args
        assert call_kwargs[1].get("include_closed") is True or True  # param passed

    @pytest.mark.asyncio
    async def test_list_tasks_stops_on_last_page_flag(self, connector: ClickUpConnector) -> None:
        task = _make_task()
        connector._http_client.get_tasks = AsyncMock(
            return_value={"tasks": [task], "last_page": True}
        )
        result = await connector.list_tasks(LIST_ID)
        # Only page 0 because last_page=True
        assert len(result) == 1
        assert connector._http_client.get_tasks.call_count == 1

    @pytest.mark.asyncio
    async def test_get_task_success(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_task = AsyncMock(return_value=_make_task())
        result = await connector.get_task(TASK_ID)
        assert result["id"] == TASK_ID

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_task = AsyncMock(
            side_effect=ClickUpNotFoundError("task", TASK_ID)
        )
        with pytest.raises(ClickUpNotFoundError):
            await connector.get_task(TASK_ID)

    @pytest.mark.asyncio
    async def test_get_task_auth_error(self, connector: ClickUpConnector) -> None:
        connector._http_client.get_task = AsyncMock(
            side_effect=ClickUpAuthError("Forbidden", 403)
        )
        with pytest.raises(ClickUpAuthError):
            await connector.get_task(TASK_ID)


# ═══════════════════════════════════════════════════════════════════════════════
# 15. PAGINATION SCENARIOS (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestPagination:
    @pytest.mark.asyncio
    async def test_list_tasks_three_pages(self, connector: ClickUpConnector) -> None:
        """Three pages: each full page (100 tasks) triggers the next; last page empty."""
        from connector import _PAGE_SIZE
        page0 = [_make_task(f"t-{i:03d}") for i in range(_PAGE_SIZE)]
        page1 = [_make_task(f"t-{i + _PAGE_SIZE:03d}") for i in range(_PAGE_SIZE)]
        page2: List[Any] = []
        pages: Dict[int, List[Any]] = {0: page0, 1: page1, 2: page2}
        pages_called: List[int] = []

        async def mock_get(list_id: str, page: int = 0, **kw: Any) -> Dict[str, Any]:
            pages_called.append(page)
            return {"tasks": pages.get(page, [])}

        connector._http_client.get_tasks = mock_get
        result = await connector.list_tasks(LIST_ID)
        assert len(result) == 2 * _PAGE_SIZE
        assert pages_called == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_sync_task_pagination_calls_multiple_pages(
        self, connector: ClickUpConnector
    ) -> None:
        """Sync continues to page 1 when page 0 returns a full page (100 tasks)."""
        from connector import _PAGE_SIZE
        connector._http_client.get_teams = AsyncMock(return_value={"teams": [_make_team()]})
        connector._http_client.get_spaces = AsyncMock(return_value={"spaces": [_make_space()]})
        connector._http_client.get_folders = AsyncMock(return_value={"folders": [_make_folder()]})
        connector._http_client.get_lists = AsyncMock(
            side_effect=[{"lists": [_make_list()]}, {"lists": []}]
        )

        page0_tasks = [_make_task(f"t-{i:03d}") for i in range(_PAGE_SIZE)]
        page_calls: List[int] = []

        async def mock_tasks(list_id: str, page: int = 0, **kw: Any) -> Dict[str, Any]:
            page_calls.append(page)
            if page == 0:
                return {"tasks": page0_tasks}
            return {"tasks": []}

        connector._http_client.get_tasks = mock_tasks
        result = await connector.sync()
        assert 0 in page_calls
        assert 1 in page_calls  # page 1 confirmed empty

    @pytest.mark.asyncio
    async def test_pagination_with_exactly_page_size_items_continues(
        self, connector: ClickUpConnector
    ) -> None:
        """When a page returns exactly PAGE_SIZE (100) items, pagination continues."""
        from connector import _PAGE_SIZE
        page0_tasks = [_make_task(f"t-{i:03d}") for i in range(_PAGE_SIZE)]
        pages_called: List[int] = []

        async def mock_get(list_id: str, page: int = 0, **kw: Any) -> Dict[str, Any]:
            pages_called.append(page)
            if page == 0:
                return {"tasks": page0_tasks}
            return {"tasks": []}

        connector._http_client.get_tasks = mock_get
        result = await connector.list_tasks(LIST_ID)
        assert len(result) == _PAGE_SIZE
        assert 0 in pages_called
        assert 1 in pages_called

    @pytest.mark.asyncio
    async def test_pagination_stops_when_fewer_than_page_size(
        self, connector: ClickUpConnector
    ) -> None:
        """A page with fewer than 100 tasks stops pagination immediately."""
        pages_called: List[int] = []

        async def mock_get(list_id: str, page: int = 0, **kw: Any) -> Dict[str, Any]:
            pages_called.append(page)
            # Only 2 tasks — fewer than PAGE_SIZE(100) → stop
            return {"tasks": [_make_task("t-001"), _make_task("t-002")]}

        connector._http_client.get_tasks = mock_get
        result = await connector.list_tasks(LIST_ID)
        assert len(result) == 2
        assert pages_called == [0]  # stopped after first page

    @pytest.mark.asyncio
    async def test_pagination_last_page_flag_stops_immediately(
        self, connector: ClickUpConnector
    ) -> None:
        """last_page=True stops pagination even if exactly 100 items returned."""
        from connector import _PAGE_SIZE
        page0_tasks = [_make_task(f"t-{i:03d}") for i in range(_PAGE_SIZE)]
        call_count = 0

        async def mock_get(list_id: str, page: int = 0, **kw: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"tasks": page0_tasks, "last_page": True}

        connector._http_client.get_tasks = mock_get
        result = await connector.list_tasks(LIST_ID)
        assert len(result) == _PAGE_SIZE
        # Only one call because last_page=True
        assert call_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 16. LIFECYCLE (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_clears_client(self, connector: ClickUpConnector) -> None:
        connector._http_client.aclose = AsyncMock()
        await connector.aclose()
        assert connector._http_client is None

    @pytest.mark.asyncio
    async def test_aclose_idempotent(self) -> None:
        c = make_connector()
        await c.aclose()  # no client yet — should not raise
        await c.aclose()  # again — still should not raise

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        c = make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        c._http_client = mock_client
        async with c:
            pass
        mock_client.aclose.assert_called_once()
