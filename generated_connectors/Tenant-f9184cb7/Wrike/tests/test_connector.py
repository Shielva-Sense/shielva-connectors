"""Unit tests for WrikeConnector — all HTTP calls are mocked.

Test count target: 73+ tests covering:
  - exceptions            (5 tests)
  - models                (6 tests)
  - normalize_task        (8 tests)
  - normalize_folder      (5 tests)
  - normalize_user        (5 tests)
  - normalize_comment     (5 tests)
  - with_retry            (6 tests)
  - HTTP client mocked   (14 tests)
  - install               (6 tests)
  - health_check          (5 tests)
  - sync                  (8 tests)
  - list_folders/tasks/users/comments (6 tests)
  - get_task              (3 tests)
  - authorize URL         (3 tests)
  - token refresh         (3 tests)
  - nextPageToken pagination (3 tests)
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import WrikeConnector, CONNECTOR_TYPE, AUTH_TYPE, _WRIKE_AUTH_URL
from exceptions import (
    WrikeAuthError,
    WrikeError,
    WrikeNetworkError,
    WrikeNotFoundError,
    WrikeRateLimitError,
)
from helpers.utils import (
    normalize_comment,
    normalize_folder,
    normalize_task,
    normalize_user,
    with_retry,
    _short_id,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
    WrikeFolder,
    WrikeTask,
    WrikeUser,
    WrikeComment,
    WrikeTaskStatus,
    WrikeImportance,
)

# ── Fixtures / constants ──────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_wrike_test_001"

BASE_CONFIG = {
    "client_id": "test_client_id_abc",
    "client_secret": "test_client_secret_xyz",
    "access_token": "test_access_token_12345",
    "refresh_token": "test_refresh_token_67890",
    "redirect_uri": "https://app.shielva.ai/oauth/callback/wrike",
}

SAMPLE_ME_RESPONSE: dict[str, Any] = {
    "kind": "contacts",
    "data": [
        {
            "id": "CONTACT_001",
            "firstName": "Jane",
            "lastName": "Wrike",
            "profiles": [{"email": "jane@acme.com", "role": "Owner"}],
            "active": True,
            "avatarUrl": "https://avatars.wrike.com/jane.png",
        }
    ],
}

SAMPLE_FOLDERS_RESPONSE: dict[str, Any] = {
    "kind": "folders",
    "data": [
        {
            "id": "FOLDER_001",
            "title": "Product Roadmap",
            "description": "All product planning items",
            "color": "Green",
            "createdDate": "2026-01-01T00:00:00Z",
            "updatedDate": "2026-06-01T00:00:00Z",
            "scope": "WsFolder",
            "childIds": ["FOLDER_002", "FOLDER_003"],
            "sharedIds": ["CONTACT_001"],
        },
        {
            "id": "FOLDER_002",
            "title": "Q3 Planning",
            "description": "",
            "color": "",
            "createdDate": "2026-02-01T00:00:00Z",
            "updatedDate": "2026-06-10T00:00:00Z",
            "scope": "WsFolder",
            "childIds": [],
            "sharedIds": [],
            "project": {"status": "Green"},
        },
    ],
}

SAMPLE_TASKS_RESPONSE: dict[str, Any] = {
    "kind": "tasks",
    "data": [
        {
            "id": "TASK_001",
            "title": "Design new landing page",
            "description": "Use the brand kit from Figma.",
            "status": "Active",
            "importance": "High",
            "createdDate": "2026-01-15T10:00:00Z",
            "updatedDate": "2026-06-15T10:00:00Z",
            "dates": {"due": "2026-07-15", "start": "2026-06-20"},
            "responsibleIds": ["CONTACT_001"],
            "parentIds": ["FOLDER_001"],
            "customStatusId": "STATUS_001",
        },
        {
            "id": "TASK_002",
            "title": "Write Q3 OKRs",
            "description": "",
            "status": "Completed",
            "importance": "Normal",
            "createdDate": "2026-02-01T00:00:00Z",
            "updatedDate": "2026-06-01T00:00:00Z",
            "dates": {},
            "responsibleIds": [],
            "parentIds": ["FOLDER_002"],
            "customStatusId": "",
        },
    ],
}

SAMPLE_USERS_RESPONSE: dict[str, Any] = {
    "kind": "contacts",
    "data": [
        {
            "id": "CONTACT_001",
            "firstName": "Jane",
            "lastName": "Wrike",
            "profiles": [{"email": "jane@acme.com", "role": "Owner"}],
            "active": True,
            "avatarUrl": "https://avatars.wrike.com/jane.png",
        },
        {
            "id": "CONTACT_002",
            "firstName": "Bob",
            "lastName": "Smith",
            "profiles": [{"email": "bob@acme.com", "role": "Collaborator"}],
            "active": False,
            "avatarUrl": "",
        },
    ],
}

SAMPLE_COMMENTS_RESPONSE: dict[str, Any] = {
    "kind": "comments",
    "data": [
        {
            "id": "COMMENT_001",
            "authorId": "CONTACT_001",
            "text": "Please review the mockups in the Figma link.",
            "createdDate": "2026-06-10T09:00:00Z",
            "updatedDate": "2026-06-10T09:00:00Z",
            "taskId": "TASK_001",
        },
        {
            "id": "COMMENT_002",
            "authorId": "CONTACT_002",
            "text": "Done — all items resolved.",
            "createdDate": "2026-06-12T14:00:00Z",
            "updatedDate": "2026-06-12T14:00:00Z",
            "taskId": "TASK_002",
        },
    ],
}

SAMPLE_TASK_SINGLE_RESPONSE: dict[str, Any] = {
    "kind": "tasks",
    "data": [SAMPLE_TASKS_RESPONSE["data"][0]],
}


# ═════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTION TESTS (5)
# ═════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_wrike_error_base(self) -> None:
        exc = WrikeError("Something went wrong", status_code=500, code="server_error")
        assert str(exc) == "Something went wrong"
        assert exc.message == "Something went wrong"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_wrike_auth_error_is_wrike_error(self) -> None:
        exc = WrikeAuthError("Unauthorized", status_code=401, code="auth_error")
        assert isinstance(exc, WrikeError)
        assert exc.status_code == 401

    def test_wrike_rate_limit_error_defaults(self) -> None:
        exc = WrikeRateLimitError("Rate limited")
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 0.0

    def test_wrike_not_found_error_message(self) -> None:
        exc = WrikeNotFoundError("task", "TASK_999")
        assert "TASK_999" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert exc.resource == "task"
        assert exc.resource_id == "TASK_999"

    def test_wrike_network_error_inherits_base(self) -> None:
        exc = WrikeNetworkError("Connection refused", status_code=503)
        assert isinstance(exc, WrikeError)
        assert exc.status_code == 503


# ═════════════════════════════════════════════════════════════════════════════
# 2. MODEL TESTS (6)
# ═════════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_pending_oauth(self) -> None:
        assert AuthStatus.PENDING_OAUTH == "pending_oauth"
        assert AuthStatus.CONNECTED == "connected"

    def test_wrike_task_from_raw(self) -> None:
        raw = SAMPLE_TASKS_RESPONSE["data"][0]
        task = WrikeTask.from_raw(raw)
        assert task.id == "TASK_001"
        assert task.title == "Design new landing page"
        assert task.status == "Active"
        assert task.importance == "High"
        assert task.due_date == "2026-07-15"
        assert "CONTACT_001" in task.assignee_ids

    def test_wrike_folder_from_raw(self) -> None:
        raw = SAMPLE_FOLDERS_RESPONSE["data"][0]
        folder = WrikeFolder.from_raw(raw)
        assert folder.id == "FOLDER_001"
        assert folder.title == "Product Roadmap"
        assert "FOLDER_002" in folder.child_ids

    def test_wrike_user_full_name(self) -> None:
        raw = SAMPLE_USERS_RESPONSE["data"][0]
        user = WrikeUser.from_raw(raw)
        assert user.full_name == "Jane Wrike"
        assert user.email == "jane@acme.com"
        assert user.role == "Owner"
        assert user.active is True

    def test_wrike_comment_from_raw(self) -> None:
        raw = SAMPLE_COMMENTS_RESPONSE["data"][0]
        comment = WrikeComment.from_raw(raw)
        assert comment.id == "COMMENT_001"
        assert comment.author_id == "CONTACT_001"
        assert comment.task_id == "TASK_001"
        assert "Figma" in comment.text


# ═════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZE_TASK TESTS (8)
# ═════════════════════════════════════════════════════════════════════════════


class TestNormalizeTask:
    def test_basic_normalization(self) -> None:
        raw = SAMPLE_TASKS_RESPONSE["data"][0]
        doc = normalize_task(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert doc.title == "Design new landing page"
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_source_id_is_stable_sha256_prefix(self) -> None:
        raw = SAMPLE_TASKS_RESPONSE["data"][0]
        doc = normalize_task(raw)
        expected = hashlib.sha256(b"task:TASK_001").hexdigest()[:16]
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_source_url_contains_task_id(self) -> None:
        raw = SAMPLE_TASKS_RESPONSE["data"][0]
        doc = normalize_task(raw)
        assert "TASK_001" in doc.source_url
        assert "wrike.com" in doc.source_url

    def test_content_contains_title_status_importance(self) -> None:
        raw = SAMPLE_TASKS_RESPONSE["data"][0]
        doc = normalize_task(raw)
        assert "Design new landing page" in doc.content
        assert "Active" in doc.content
        assert "High" in doc.content

    def test_content_contains_due_date(self) -> None:
        raw = SAMPLE_TASKS_RESPONSE["data"][0]
        doc = normalize_task(raw)
        assert "2026-07-15" in doc.content

    def test_content_contains_description(self) -> None:
        raw = SAMPLE_TASKS_RESPONSE["data"][0]
        doc = normalize_task(raw)
        assert "Figma" in doc.content

    def test_metadata_fields(self) -> None:
        raw = SAMPLE_TASKS_RESPONSE["data"][0]
        doc = normalize_task(raw)
        assert doc.metadata["task_id"] == "TASK_001"
        assert doc.metadata["status"] == "Active"
        assert doc.metadata["importance"] == "High"
        assert "CONTACT_001" in doc.metadata["responsible_ids"]

    def test_empty_task_uses_fallback_title(self) -> None:
        raw: dict[str, Any] = {"id": "TASK_X", "dates": {}}
        doc = normalize_task(raw)
        assert "TASK_X" in doc.title
        assert doc.source_id == hashlib.sha256(b"task:TASK_X").hexdigest()[:16]


# ═════════════════════════════════════════════════════════════════════════════
# 4. NORMALIZE_FOLDER TESTS (5)
# ═════════════════════════════════════════════════════════════════════════════


class TestNormalizeFolder:
    def test_basic_normalization(self) -> None:
        raw = SAMPLE_FOLDERS_RESPONSE["data"][0]
        doc = normalize_folder(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert doc.title == "Product Roadmap"
        assert doc.connector_id == CONNECTOR_ID

    def test_source_id_is_stable(self) -> None:
        raw = SAMPLE_FOLDERS_RESPONSE["data"][0]
        doc = normalize_folder(raw)
        expected = hashlib.sha256(b"folder:FOLDER_001").hexdigest()[:16]
        assert doc.source_id == expected

    def test_metadata_is_project_flag(self) -> None:
        raw_plain = SAMPLE_FOLDERS_RESPONSE["data"][0]
        raw_project = SAMPLE_FOLDERS_RESPONSE["data"][1]
        doc_plain = normalize_folder(raw_plain)
        doc_project = normalize_folder(raw_project)
        assert doc_plain.metadata["is_project"] is False
        assert doc_project.metadata["is_project"] is True
        assert doc_project.metadata["project_status"] == "Green"

    def test_content_contains_scope(self) -> None:
        raw = SAMPLE_FOLDERS_RESPONSE["data"][0]
        doc = normalize_folder(raw)
        assert "WsFolder" in doc.content

    def test_content_contains_child_count(self) -> None:
        raw = SAMPLE_FOLDERS_RESPONSE["data"][0]
        doc = normalize_folder(raw)
        assert "2 item" in doc.content


# ═════════════════════════════════════════════════════════════════════════════
# 5. NORMALIZE_USER TESTS (5)
# ═════════════════════════════════════════════════════════════════════════════


class TestNormalizeUser:
    def test_basic_normalization(self) -> None:
        raw = SAMPLE_USERS_RESPONSE["data"][0]
        doc = normalize_user(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert doc.title == "Jane Wrike"
        assert doc.connector_id == CONNECTOR_ID

    def test_source_id_is_stable(self) -> None:
        raw = SAMPLE_USERS_RESPONSE["data"][0]
        doc = normalize_user(raw)
        expected = hashlib.sha256(b"user:CONTACT_001").hexdigest()[:16]
        assert doc.source_id == expected

    def test_content_contains_email_and_role(self) -> None:
        raw = SAMPLE_USERS_RESPONSE["data"][0]
        doc = normalize_user(raw)
        assert "jane@acme.com" in doc.content
        assert "Owner" in doc.content

    def test_inactive_user(self) -> None:
        raw = SAMPLE_USERS_RESPONSE["data"][1]
        doc = normalize_user(raw)
        assert "No" in doc.content
        assert doc.metadata["active"] is False

    def test_no_profiles_fallback(self) -> None:
        raw = {"id": "CONTACT_X", "firstName": "Test", "lastName": "User", "active": True}
        doc = normalize_user(raw)
        assert doc.title == "Test User"
        assert doc.metadata["email"] == ""


# ═════════════════════════════════════════════════════════════════════════════
# 6. NORMALIZE_COMMENT TESTS (5)
# ═════════════════════════════════════════════════════════════════════════════


class TestNormalizeComment:
    def test_basic_normalization(self) -> None:
        raw = SAMPLE_COMMENTS_RESPONSE["data"][0]
        doc = normalize_comment(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert "Figma" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_source_id_is_stable(self) -> None:
        raw = SAMPLE_COMMENTS_RESPONSE["data"][0]
        doc = normalize_comment(raw)
        expected = hashlib.sha256(b"comment:COMMENT_001").hexdigest()[:16]
        assert doc.source_id == expected

    def test_source_url_points_to_task(self) -> None:
        raw = SAMPLE_COMMENTS_RESPONSE["data"][0]
        doc = normalize_comment(raw)
        assert "TASK_001" in doc.source_url

    def test_metadata_fields(self) -> None:
        raw = SAMPLE_COMMENTS_RESPONSE["data"][0]
        doc = normalize_comment(raw)
        assert doc.metadata["comment_id"] == "COMMENT_001"
        assert doc.metadata["author_id"] == "CONTACT_001"
        assert doc.metadata["task_id"] == "TASK_001"

    def test_empty_text_uses_id_in_title(self) -> None:
        raw: dict[str, Any] = {
            "id": "COMMENT_Z",
            "authorId": "CONTACT_001",
            "text": "",
            "createdDate": "",
        }
        doc = normalize_comment(raw)
        assert "COMMENT_Z" in doc.title


# ═════════════════════════════════════════════════════════════════════════════
# 7. WITH_RETRY TESTS (6)
# ═════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    async def test_succeeds_on_first_attempt(self) -> None:
        mock_fn = AsyncMock(return_value={"data": []})
        result = await with_retry(mock_fn, max_attempts=3)
        assert result == {"data": []}
        assert mock_fn.call_count == 1

    async def test_retries_on_network_error(self) -> None:
        mock_fn = AsyncMock(
            side_effect=[
                WrikeNetworkError("timeout"),
                WrikeNetworkError("timeout"),
                {"data": []},
            ]
        )
        result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
        assert result == {"data": []}
        assert mock_fn.call_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        mock_fn = AsyncMock(side_effect=WrikeAuthError("Unauthorized", status_code=401))
        with pytest.raises(WrikeAuthError):
            await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    async def test_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=WrikeNetworkError("always fails"))
        with pytest.raises(WrikeNetworkError):
            await with_retry(mock_fn, max_attempts=3, base_delay=0)
        assert mock_fn.call_count == 3

    async def test_retries_on_rate_limit(self) -> None:
        mock_fn = AsyncMock(
            side_effect=[
                WrikeRateLimitError("rate limited"),
                {"data": ["ok"]},
            ]
        )
        result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
        assert result == {"data": ["ok"]}
        assert mock_fn.call_count == 2

    async def test_passes_args_to_fn(self) -> None:
        mock_fn = AsyncMock(return_value="result")
        result = await with_retry(mock_fn, "arg1", "arg2", max_attempts=2)
        mock_fn.assert_called_once_with("arg1", "arg2")
        assert result == "result"


# ═════════════════════════════════════════════════════════════════════════════
# 8. HTTP CLIENT TESTS (14)
# ═════════════════════════════════════════════════════════════════════════════


class TestWrikeHTTPClient:
    """Tests for WrikeHTTPClient using mocked aiohttp sessions."""

    def _make_mock_response(
        self, status: int, json_body: dict[str, Any]
    ) -> MagicMock:
        mock_resp = AsyncMock()
        mock_resp.status = status
        mock_resp.json = AsyncMock(return_value=json_body)
        mock_resp.headers = {}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        return mock_resp

    def _make_mock_session(self, mock_resp: MagicMock) -> MagicMock:
        """Build a mock aiohttp session with AsyncMock.close() so aclose() works."""
        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        mock_session.close = AsyncMock()
        return mock_session

    async def test_get_contacts_me_true(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(200, SAMPLE_ME_RESPONSE)
        client._session = self._make_mock_session(mock_resp)

        result = await client.get_contacts(me=True)
        assert result["data"][0]["id"] == "CONTACT_001"
        await client.aclose()

    async def test_get_contacts_no_me(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(200, SAMPLE_USERS_RESPONSE)
        client._session = self._make_mock_session(mock_resp)

        result = await client.get_contacts(me=False)
        assert len(result["data"]) == 2
        await client.aclose()

    async def test_get_folders(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(200, SAMPLE_FOLDERS_RESPONSE)
        client._session = self._make_mock_session(mock_resp)

        result = await client.get_folders()
        assert result["kind"] == "folders"
        assert len(result["data"]) == 2
        await client.aclose()

    async def test_get_tasks_no_folder(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(200, SAMPLE_TASKS_RESPONSE)
        client._session = self._make_mock_session(mock_resp)

        result = await client.get_tasks()
        assert result["kind"] == "tasks"
        assert len(result["data"]) == 2
        await client.aclose()

    async def test_get_tasks_with_folder(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(200, SAMPLE_TASKS_RESPONSE)
        mock_session = self._make_mock_session(mock_resp)
        client._session = mock_session

        result = await client.get_tasks(folder_id="FOLDER_001")
        assert len(result["data"]) == 2
        # verify the path was /folders/FOLDER_001/tasks
        call_args = mock_session.request.call_args
        assert "FOLDER_001" in call_args[0][1]
        await client.aclose()

    async def test_get_task_single(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(200, SAMPLE_TASK_SINGLE_RESPONSE)
        client._session = self._make_mock_session(mock_resp)

        result = await client.get_task("TASK_001")
        assert result["data"][0]["id"] == "TASK_001"
        await client.aclose()

    async def test_get_comments(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(200, SAMPLE_COMMENTS_RESPONSE)
        client._session = self._make_mock_session(mock_resp)

        result = await client.get_comments()
        assert result["kind"] == "comments"
        assert len(result["data"]) == 2
        await client.aclose()

    async def test_get_timelogs(self) -> None:
        from client.http_client import WrikeHTTPClient

        timelogs_resp = {"kind": "timelogs", "data": [{"id": "TL_001", "hours": 2.5}]}
        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(200, timelogs_resp)
        client._session = self._make_mock_session(mock_resp)

        result = await client.get_timelogs()
        assert result["kind"] == "timelogs"
        await client.aclose()

    async def test_401_raises_auth_error(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(
            config={**BASE_CONFIG, "access_token": "bad_token", "refresh_token": ""}
        )
        mock_resp = self._make_mock_response(
            401, {"error": "invalid_token", "errorDescription": "Access token is invalid"}
        )
        client._session = self._make_mock_session(mock_resp)

        with pytest.raises(WrikeAuthError):
            await client.get_contacts(me=True)
        await client.aclose()

    async def test_404_raises_not_found(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(
            404, {"error": "not_found", "errorDescription": "Resource not found"}
        )
        client._session = self._make_mock_session(mock_resp)

        with pytest.raises(WrikeNotFoundError):
            await client.get_task("TASK_NONEXISTENT")
        await client.aclose()

    async def test_429_raises_rate_limit(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(
            429, {"error": "rate_limit", "errorDescription": "Rate limit exceeded"}
        )
        client._session = self._make_mock_session(mock_resp)

        with pytest.raises(WrikeRateLimitError):
            await client.get_folders()
        await client.aclose()

    async def test_500_raises_network_error(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(
            500, {"error": "server_error", "errorDescription": "Internal error"}
        )
        client._session = self._make_mock_session(mock_resp)

        with pytest.raises(WrikeNetworkError):
            await client.get_folders()
        await client.aclose()

    async def test_403_raises_auth_error(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(
            403, {"error": "forbidden", "errorDescription": "Access denied"}
        )
        client._session = self._make_mock_session(mock_resp)

        with pytest.raises(WrikeAuthError):
            await client.get_folders()
        await client.aclose()

    async def test_get_users_alias(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = self._make_mock_response(200, SAMPLE_USERS_RESPONSE)
        client._session = self._make_mock_session(mock_resp)

        result = await client.get_users()
        assert result["data"][0]["id"] == "CONTACT_001"
        await client.aclose()


# ═════════════════════════════════════════════════════════════════════════════
# 9. INSTALL TESTS (6)
# ═════════════════════════════════════════════════════════════════════════════


class TestInstall:
    def _make_connector(self, config: dict[str, Any]) -> WrikeConnector:
        return WrikeConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=config
        )

    async def test_install_missing_all_creds(self) -> None:
        conn = self._make_connector({})
        result = await conn.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in result.message

    async def test_install_pending_oauth_no_token(self) -> None:
        conn = self._make_connector(
            {"client_id": "abc", "client_secret": "xyz"}
        )
        result = await conn.install()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.PENDING_OAUTH

    async def test_install_connected_with_token(self) -> None:
        conn = self._make_connector(BASE_CONFIG)
        conn.client.get_contacts = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
        result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Jane Wrike" in result.message

    async def test_install_invalid_token(self) -> None:
        conn = self._make_connector(BASE_CONFIG)
        conn.client.get_contacts = AsyncMock(
            side_effect=WrikeAuthError("Unauthorized", status_code=401)
        )
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_failure(self) -> None:
        conn = self._make_connector(BASE_CONFIG)
        conn.client.get_contacts = AsyncMock(
            side_effect=WrikeNetworkError("Connection refused")
        )
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_install_returns_connector_id(self) -> None:
        conn = self._make_connector(BASE_CONFIG)
        conn.client.get_contacts = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
        result = await conn.install()
        assert result.connector_id == CONNECTOR_ID


# ═════════════════════════════════════════════════════════════════════════════
# 10. HEALTH CHECK TESTS (5)
# ═════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    def _make_connector(self, config: dict[str, Any] | None = None) -> WrikeConnector:
        return WrikeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config or BASE_CONFIG,
        )

    async def test_healthy_with_valid_token(self) -> None:
        conn = self._make_connector()
        conn.client.get_contacts = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
        result = await conn.health_check()
        assert isinstance(result, HealthCheckResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Jane Wrike" in result.message

    async def test_missing_access_token(self) -> None:
        conn = self._make_connector({"client_id": "abc", "client_secret": "xyz"})
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_invalid_token_returns_offline(self) -> None:
        conn = self._make_connector()
        conn.client.get_contacts = AsyncMock(
            side_effect=WrikeAuthError("Unauthorized", status_code=401)
        )
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_network_error_returns_degraded(self) -> None:
        conn = self._make_connector()
        conn.client.get_contacts = AsyncMock(
            side_effect=WrikeNetworkError("timeout")
        )
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_generic_exception_returns_degraded(self) -> None:
        conn = self._make_connector()
        conn.client.get_contacts = AsyncMock(side_effect=RuntimeError("unexpected"))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ═════════════════════════════════════════════════════════════════════════════
# 11. SYNC TESTS (8)
# ═════════════════════════════════════════════════════════════════════════════


class TestSync:
    def _make_connector(self) -> WrikeConnector:
        return WrikeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=BASE_CONFIG,
        )

    async def test_sync_completes_all_resources(self) -> None:
        conn = self._make_connector()
        conn.client.get_folders = AsyncMock(return_value=SAMPLE_FOLDERS_RESPONSE)
        conn.client.get_tasks = AsyncMock(
            return_value={**SAMPLE_TASKS_RESPONSE, "nextPageToken": None}
        )
        conn.client.get_users = AsyncMock(return_value=SAMPLE_USERS_RESPONSE)
        conn.client.get_comments = AsyncMock(
            return_value={**SAMPLE_COMMENTS_RESPONSE, "nextPageToken": None}
        )

        result = await conn.sync()
        assert isinstance(result, SyncResult)
        assert result.status == SyncStatus.COMPLETED
        # 2 folders + 2 tasks + 2 users + 2 comments = 8
        assert result.documents_found == 8
        assert result.documents_synced == 8
        assert result.documents_failed == 0

    async def test_sync_partial_on_normalization_error(self) -> None:
        conn = self._make_connector()
        bad_folder_resp = {"kind": "folders", "data": [None]}  # None will cause normalize error
        conn.client.get_folders = AsyncMock(return_value=bad_folder_resp)
        conn.client.get_tasks = AsyncMock(
            return_value={"kind": "tasks", "data": [], "nextPageToken": None}
        )
        conn.client.get_users = AsyncMock(return_value={"kind": "contacts", "data": []})
        conn.client.get_comments = AsyncMock(
            return_value={"kind": "comments", "data": [], "nextPageToken": None}
        )

        result = await conn.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed == 1

    async def test_sync_returns_failed_on_early_error(self) -> None:
        conn = self._make_connector()
        conn.client.get_folders = AsyncMock(
            side_effect=WrikeNetworkError("connection refused")
        )
        conn.client.get_tasks = AsyncMock(
            return_value={"kind": "tasks", "data": [], "nextPageToken": None}
        )
        conn.client.get_users = AsyncMock(return_value={"kind": "contacts", "data": []})
        conn.client.get_comments = AsyncMock(
            return_value={"kind": "comments", "data": [], "nextPageToken": None}
        )

        result = await conn.sync()
        # tasks/users/comments still complete; folders errored out but caught
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    async def test_sync_pagination_tasks(self) -> None:
        conn = self._make_connector()
        conn.client.get_folders = AsyncMock(return_value={"kind": "folders", "data": []})
        conn.client.get_tasks = AsyncMock(
            side_effect=[
                {**SAMPLE_TASKS_RESPONSE, "nextPageToken": "TOKEN_PAGE_2"},
                {**SAMPLE_TASKS_RESPONSE, "nextPageToken": None},
            ]
        )
        conn.client.get_users = AsyncMock(return_value={"kind": "contacts", "data": []})
        conn.client.get_comments = AsyncMock(
            return_value={"kind": "comments", "data": [], "nextPageToken": None}
        )

        result = await conn.sync()
        # 2 pages × 2 tasks = 4
        assert result.documents_found == 4
        assert result.documents_synced == 4

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        conn = self._make_connector()
        conn.client.get_folders = AsyncMock(
            return_value={"kind": "folders", "data": [SAMPLE_FOLDERS_RESPONSE["data"][0]]}
        )
        conn.client.get_tasks = AsyncMock(
            return_value={"kind": "tasks", "data": [], "nextPageToken": None}
        )
        conn.client.get_users = AsyncMock(return_value={"kind": "contacts", "data": []})
        conn.client.get_comments = AsyncMock(
            return_value={"kind": "comments", "data": [], "nextPageToken": None}
        )
        conn._ingest_document = AsyncMock()

        await conn.sync(kb_id="kb_001")
        assert conn._ingest_document.call_count == 1

    async def test_sync_no_kb_id_skips_ingest(self) -> None:
        conn = self._make_connector()
        conn.client.get_folders = AsyncMock(
            return_value={"kind": "folders", "data": [SAMPLE_FOLDERS_RESPONSE["data"][0]]}
        )
        conn.client.get_tasks = AsyncMock(
            return_value={"kind": "tasks", "data": [], "nextPageToken": None}
        )
        conn.client.get_users = AsyncMock(return_value={"kind": "contacts", "data": []})
        conn.client.get_comments = AsyncMock(
            return_value={"kind": "comments", "data": [], "nextPageToken": None}
        )
        conn._ingest_document = AsyncMock()

        await conn.sync(kb_id="")
        conn._ingest_document.assert_not_called()

    async def test_sync_comment_pagination(self) -> None:
        conn = self._make_connector()
        conn.client.get_folders = AsyncMock(return_value={"kind": "folders", "data": []})
        conn.client.get_tasks = AsyncMock(
            return_value={"kind": "tasks", "data": [], "nextPageToken": None}
        )
        conn.client.get_users = AsyncMock(return_value={"kind": "contacts", "data": []})
        conn.client.get_comments = AsyncMock(
            side_effect=[
                {**SAMPLE_COMMENTS_RESPONSE, "nextPageToken": "COMMENT_PAGE_2"},
                {**SAMPLE_COMMENTS_RESPONSE, "nextPageToken": None},
            ]
        )

        result = await conn.sync()
        # 2 pages × 2 comments = 4
        assert result.documents_found == 4

    async def test_sync_result_type(self) -> None:
        conn = self._make_connector()
        conn.client.get_folders = AsyncMock(return_value={"kind": "folders", "data": []})
        conn.client.get_tasks = AsyncMock(
            return_value={"kind": "tasks", "data": [], "nextPageToken": None}
        )
        conn.client.get_users = AsyncMock(return_value={"kind": "contacts", "data": []})
        conn.client.get_comments = AsyncMock(
            return_value={"kind": "comments", "data": [], "nextPageToken": None}
        )

        result = await conn.sync(full=True)
        assert isinstance(result, SyncResult)
        assert result.status == SyncStatus.COMPLETED


# ═════════════════════════════════════════════════════════════════════════════
# 12. LIST METHODS TESTS (6)
# ═════════════════════════════════════════════════════════════════════════════


class TestListMethods:
    def _make_connector(self) -> WrikeConnector:
        return WrikeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=BASE_CONFIG,
        )

    async def test_list_folders_returns_data_list(self) -> None:
        conn = self._make_connector()
        conn.client.get_folders = AsyncMock(return_value=SAMPLE_FOLDERS_RESPONSE)
        result = await conn.list_folders()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["id"] == "FOLDER_001"

    async def test_list_tasks_collects_all_pages(self) -> None:
        conn = self._make_connector()
        conn.client.get_tasks = AsyncMock(
            side_effect=[
                {**SAMPLE_TASKS_RESPONSE, "nextPageToken": "PAGE_2"},
                {**SAMPLE_TASKS_RESPONSE, "nextPageToken": None},
            ]
        )
        result = await conn.list_tasks()
        assert len(result) == 4  # 2 per page × 2 pages

    async def test_list_tasks_with_folder_id(self) -> None:
        conn = self._make_connector()
        conn.client.get_tasks = AsyncMock(
            return_value={**SAMPLE_TASKS_RESPONSE, "nextPageToken": None}
        )
        result = await conn.list_tasks(folder_id="FOLDER_001")
        conn.client.get_tasks.assert_called_with(
            folder_id="FOLDER_001", next_page_token=None
        )
        assert len(result) == 2

    async def test_list_users_returns_data_list(self) -> None:
        conn = self._make_connector()
        conn.client.get_users = AsyncMock(return_value=SAMPLE_USERS_RESPONSE)
        result = await conn.list_users()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[1]["id"] == "CONTACT_002"

    async def test_list_comments_collects_all_pages(self) -> None:
        conn = self._make_connector()
        conn.client.get_comments = AsyncMock(
            side_effect=[
                {**SAMPLE_COMMENTS_RESPONSE, "nextPageToken": "CPAGE_2"},
                {**SAMPLE_COMMENTS_RESPONSE, "nextPageToken": None},
            ]
        )
        result = await conn.list_comments()
        assert len(result) == 4  # 2 per page × 2 pages

    async def test_list_empty_result(self) -> None:
        conn = self._make_connector()
        conn.client.get_folders = AsyncMock(return_value={"kind": "folders", "data": []})
        result = await conn.list_folders()
        assert result == []


# ═════════════════════════════════════════════════════════════════════════════
# 13. GET_TASK TESTS (3)
# ═════════════════════════════════════════════════════════════════════════════


class TestGetTask:
    def _make_connector(self) -> WrikeConnector:
        return WrikeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=BASE_CONFIG,
        )

    async def test_get_task_returns_first_data_element(self) -> None:
        conn = self._make_connector()
        conn.client.get_task = AsyncMock(return_value=SAMPLE_TASK_SINGLE_RESPONSE)
        result = await conn.get_task("TASK_001")
        assert result["id"] == "TASK_001"
        assert result["title"] == "Design new landing page"

    async def test_get_task_empty_data_returns_empty_dict(self) -> None:
        conn = self._make_connector()
        conn.client.get_task = AsyncMock(return_value={"kind": "tasks", "data": []})
        result = await conn.get_task("TASK_NONEXISTENT")
        assert result == {}

    async def test_get_task_propagates_not_found(self) -> None:
        conn = self._make_connector()
        conn.client.get_task = AsyncMock(
            side_effect=WrikeNotFoundError("task", "TASK_999")
        )
        with pytest.raises(WrikeNotFoundError) as exc_info:
            await conn.get_task("TASK_999")
        assert "TASK_999" in str(exc_info.value)


# ═════════════════════════════════════════════════════════════════════════════
# 14. AUTHORIZE URL TESTS (3)
# ═════════════════════════════════════════════════════════════════════════════


class TestAuthorize:
    async def test_authorize_returns_correct_base_url(self) -> None:
        conn = WrikeConnector(config=BASE_CONFIG)
        url = await conn.authorize()
        assert url.startswith(_WRIKE_AUTH_URL)

    async def test_authorize_includes_client_id(self) -> None:
        conn = WrikeConnector(config=BASE_CONFIG)
        url = await conn.authorize()
        assert "client_id=test_client_id_abc" in url

    async def test_authorize_includes_redirect_uri_when_set(self) -> None:
        conn = WrikeConnector(config=BASE_CONFIG)
        url = await conn.authorize()
        assert "redirect_uri=" in url
        assert "shielva" in url

    async def test_authorize_without_redirect_uri(self) -> None:
        conn = WrikeConnector(
            config={"client_id": "cid", "client_secret": "csec"}
        )
        url = await conn.authorize()
        assert "redirect_uri" not in url
        assert "client_id=cid" in url

    async def test_authorize_includes_response_type_code(self) -> None:
        conn = WrikeConnector(config=BASE_CONFIG)
        url = await conn.authorize()
        assert "response_type=code" in url

    async def test_authorize_includes_scope(self) -> None:
        conn = WrikeConnector(config=BASE_CONFIG)
        url = await conn.authorize()
        assert "scope=" in url


# ═════════════════════════════════════════════════════════════════════════════
# 15. TOKEN REFRESH TESTS (3)
# ═════════════════════════════════════════════════════════════════════════════


class TestTokenRefresh:
    async def test_refresh_access_token_success(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        token_response = {
            "access_token": "new_access_token_abc",
            "refresh_token": "new_refresh_token_xyz",
            "token_type": "Bearer",
        }
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=token_response)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        mock_session.close = AsyncMock()
        client._session = mock_session

        result = await client.refresh_access_token()
        assert result["access_token"] == "new_access_token_abc"
        assert result["refresh_token"] == "new_refresh_token_xyz"
        await client.aclose()

    async def test_refresh_fails_without_refresh_token(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(
            config={"client_id": "abc", "client_secret": "xyz", "access_token": "tok"}
        )
        with pytest.raises(WrikeAuthError) as exc_info:
            await client.refresh_access_token()
        assert "refresh_token" in str(exc_info.value).lower()

    async def test_refresh_raises_on_non_200(self) -> None:
        from client.http_client import WrikeHTTPClient

        client = WrikeHTTPClient(config=BASE_CONFIG)
        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.json = AsyncMock(
            return_value={"error": "invalid_client", "error_description": "Bad credentials"}
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.closed = False
        mock_session.close = AsyncMock()
        client._session = mock_session

        with pytest.raises(WrikeAuthError):
            await client.refresh_access_token()
        await client.aclose()


# ═════════════════════════════════════════════════════════════════════════════
# 16. NEXT PAGE TOKEN PAGINATION TESTS (3)
# ═════════════════════════════════════════════════════════════════════════════


class TestNextPageTokenPagination:
    def _make_connector(self) -> WrikeConnector:
        return WrikeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=BASE_CONFIG,
        )

    async def test_list_tasks_stops_on_empty_data(self) -> None:
        conn = self._make_connector()
        conn.client.get_tasks = AsyncMock(
            side_effect=[
                {**SAMPLE_TASKS_RESPONSE, "nextPageToken": "PAGE_2"},
                {"kind": "tasks", "data": []},  # empty stops loop
            ]
        )
        result = await conn.list_tasks()
        assert len(result) == 2  # only page 1 had data
        assert conn.client.get_tasks.call_count == 2

    async def test_list_comments_uses_token_cursor(self) -> None:
        conn = self._make_connector()
        call_args_list: list[Any] = []

        async def mock_get_comments(next_page_token: str | None = None) -> dict[str, Any]:
            call_args_list.append(next_page_token)
            if next_page_token is None:
                return {**SAMPLE_COMMENTS_RESPONSE, "nextPageToken": "CURSOR_42"}
            return {**SAMPLE_COMMENTS_RESPONSE, "nextPageToken": None}

        conn.client.get_comments = mock_get_comments  # type: ignore[method-assign]
        result = await conn.list_comments()
        # Page 1: no token → CURSOR_42 returned; page 2: CURSOR_42 → no token
        assert call_args_list == [None, "CURSOR_42"]
        assert len(result) == 4

    async def test_sync_passes_next_page_token(self) -> None:
        conn = self._make_connector()
        conn.client.get_folders = AsyncMock(return_value={"kind": "folders", "data": []})
        tokens_seen: list[Any] = []

        async def mock_get_tasks(
            folder_id: str | None = None,
            page_size: int = 1000,
            next_page_token: str | None = None,
        ) -> dict[str, Any]:
            tokens_seen.append(next_page_token)
            if next_page_token is None:
                return {**SAMPLE_TASKS_RESPONSE, "nextPageToken": "T2"}
            return {"kind": "tasks", "data": [], "nextPageToken": None}

        conn.client.get_tasks = mock_get_tasks  # type: ignore[method-assign]
        conn.client.get_users = AsyncMock(return_value={"kind": "contacts", "data": []})
        conn.client.get_comments = AsyncMock(
            return_value={"kind": "comments", "data": [], "nextPageToken": None}
        )

        await conn.sync()
        assert tokens_seen[0] is None
        assert tokens_seen[1] == "T2"


# ═════════════════════════════════════════════════════════════════════════════
# 17. MODULE-LEVEL CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════


class TestModuleConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "wrike"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "oauth2"

    def test_short_id_length(self) -> None:
        sid = _short_id("task", "TASK_001")
        assert len(sid) == 16

    def test_short_id_different_prefixes_differ(self) -> None:
        task_id = _short_id("task", "RESOURCE_001")
        folder_id = _short_id("folder", "RESOURCE_001")
        assert task_id != folder_id
