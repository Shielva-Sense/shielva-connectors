"""Tests for the Monday.com connector — no live API calls.

60+ async tests covering:
- Exception hierarchy
- Models (enums + dataclasses)
- normalize_board / normalize_item (stable IDs)
- with_retry (success, retries, auth bypass, exhaustion)
- execute_query (HTTP errors, GraphQL errors, success)
- _raise_for_status (each status code)
- _check_graphql_errors (each error class)
- All high-level HTTP client methods
- Connector: install, health_check, sync
- Connector: list_boards, get_board, list_board_items (cursor pagination)
- Connector: list_teams, list_users, get_item
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure package root on sys.path so relative imports work
_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    MondayComAuthError,
    MondayComError,
    MondayComNetworkError,
    MondayComNotFoundError,
    MondayComRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)
from helpers.utils import normalize_board, normalize_item, with_retry
from client.http_client import MondayComHTTPClient
from connector import MondayComConnector

TENANT = "test-tenant"
CONNECTOR_ID = "monday_com_test"
API_KEY = "test_api_key_abc123"


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_board(
    board_id: str = "12345",
    name: str = "Marketing Board",
    description: str = "Marketing tasks",
    state: str = "active",
) -> Dict[str, Any]:
    return {"id": board_id, "name": name, "description": description, "state": state}


def _make_board_detailed(board_id: str = "12345") -> Dict[str, Any]:
    return {
        "id": board_id,
        "name": "Engineering Sprint",
        "description": "Sprint planning",
        "state": "active",
        "groups": [{"id": "g1", "title": "To Do"}, {"id": "g2", "title": "Done"}],
        "columns": [
            {"id": "name", "title": "Name", "type": "name"},
            {"id": "status", "title": "Status", "type": "color"},
        ],
    }


def _make_item(
    item_id: str = "99001",
    name: str = "Design new logo",
    column_values: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "column_values": column_values
        or [
            {"id": "status", "text": "In Progress"},
            {"id": "person", "text": "Alice"},
        ],
    }


def _make_connector(config: Optional[Dict[str, Any]] = None) -> MondayComConnector:
    return MondayComConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config=config or {"api_key": API_KEY},
    )


def _make_aiohttp_mock(status: int = 200, body: Optional[Dict[str, Any]] = None):
    """Build a properly nested aiohttp.ClientSession async-context-manager mock."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=body or {})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_post_ctx = MagicMock()
    mock_post_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_post_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_post_ctx)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session


# ─────────────────────────────────────────────────────────────────────────────
# 1. Exception hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptions:
    def test_monday_com_error_is_exception(self) -> None:
        exc = MondayComError("base error")
        assert isinstance(exc, Exception)
        assert str(exc) == "base error"

    def test_auth_error_inherits_monday_com_error(self) -> None:
        exc = MondayComAuthError("bad token")
        assert isinstance(exc, MondayComError)
        assert isinstance(exc, Exception)

    def test_network_error_inherits_monday_com_error(self) -> None:
        exc = MondayComNetworkError("timeout")
        assert isinstance(exc, MondayComError)

    def test_rate_limit_error_inherits_monday_com_error(self) -> None:
        exc = MondayComRateLimitError("too many requests")
        assert isinstance(exc, MondayComError)

    def test_not_found_error_inherits_monday_com_error(self) -> None:
        exc = MondayComNotFoundError("board not found")
        assert isinstance(exc, MondayComError)

    def test_all_exceptions_carry_messages(self) -> None:
        errors = [
            MondayComError("a"),
            MondayComAuthError("b"),
            MondayComNetworkError("c"),
            MondayComRateLimitError("d"),
            MondayComNotFoundError("e"),
        ]
        for exc in errors:
            assert str(exc) != ""

    def test_exception_hierarchy_is_exact(self) -> None:
        # Only the four subclasses should directly inherit MondayComError
        assert issubclass(MondayComAuthError, MondayComError)
        assert issubclass(MondayComNetworkError, MondayComError)
        assert issubclass(MondayComRateLimitError, MondayComError)
        assert issubclass(MondayComNotFoundError, MondayComError)
        # They do NOT inherit from each other
        assert not issubclass(MondayComAuthError, MondayComNetworkError)
        assert not issubclass(MondayComRateLimitError, MondayComAuthError)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Models
# ─────────────────────────────────────────────────────────────────────────────

class TestModels:
    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_install_result_defaults(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="c1",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED
        assert r.connector_id == "c1"
        assert r.message == ""

    def test_install_result_with_message(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.MISSING_CREDENTIALS,
            connector_id="c2",
            message="api_key is required",
        )
        assert r.message == "api_key is required"

    def test_health_check_result_fields(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Connected as: Alice",
        )
        assert "Alice" in r.message
        assert r.health == ConnectorHealth.HEALTHY

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_connector_document_source_and_type(self) -> None:
        doc = ConnectorDocument(id="abc", title="T", content="C")
        assert doc.source == "monday_com"
        assert doc.type == "work_item"
        assert doc.metadata == {}

    def test_connector_document_custom_type(self) -> None:
        doc = ConnectorDocument(id="b1", title="Board", content="...", type="board")
        assert doc.type == "board"


# ─────────────────────────────────────────────────────────────────────────────
# 3. normalize_board
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeBoard:
    def _expected_id(self, board_id: str) -> str:
        return hashlib.sha256(f"board:{board_id}".encode()).hexdigest()[:16]

    def test_stable_id_from_board_id(self) -> None:
        board = _make_board(board_id="12345")
        doc = normalize_board(board)
        assert doc.id == self._expected_id("12345")

    def test_id_is_16_hex_chars(self) -> None:
        doc = normalize_board(_make_board(board_id="999"))
        assert len(doc.id) == 16
        assert all(c in "0123456789abcdef" for c in doc.id)

    def test_id_different_for_different_boards(self) -> None:
        doc1 = normalize_board(_make_board(board_id="1"))
        doc2 = normalize_board(_make_board(board_id="2"))
        assert doc1.id != doc2.id

    def test_id_stable_across_calls(self) -> None:
        board = _make_board(board_id="42")
        assert normalize_board(board).id == normalize_board(board).id

    def test_type_is_board(self) -> None:
        doc = normalize_board(_make_board())
        assert doc.type == "board"

    def test_source_is_monday_com(self) -> None:
        doc = normalize_board(_make_board())
        assert doc.source == "monday_com"

    def test_title_includes_board_name(self) -> None:
        doc = normalize_board(_make_board(name="Engineering Sprint"))
        assert "Engineering Sprint" in doc.title

    def test_content_includes_state(self) -> None:
        doc = normalize_board(_make_board(state="active"))
        assert "active" in doc.content

    def test_content_includes_description(self) -> None:
        doc = normalize_board(_make_board(description="Sprint board for Q3"))
        assert "Sprint board for Q3" in doc.content

    def test_content_includes_groups(self) -> None:
        board = _make_board()
        board["groups"] = [{"id": "g1", "title": "To Do"}]
        doc = normalize_board(board)
        assert "To Do" in doc.content

    def test_content_includes_columns(self) -> None:
        board = _make_board()
        board["columns"] = [{"id": "status", "title": "Status", "type": "color"}]
        doc = normalize_board(board)
        assert "Status" in doc.content

    def test_metadata_has_required_fields(self) -> None:
        board = _make_board(board_id="42", name="My Board", state="active")
        doc = normalize_board(board)
        assert doc.metadata["board_id"] == "42"
        assert doc.metadata["board_name"] == "My Board"
        assert doc.metadata["state"] == "active"

    def test_empty_description_excluded_from_content(self) -> None:
        board = _make_board(description="")
        doc = normalize_board(board)
        assert "Description:" not in doc.content


# ─────────────────────────────────────────────────────────────────────────────
# 4. normalize_item
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeItem:
    def _expected_id(self, item_id: str) -> str:
        return hashlib.sha256(f"item:{item_id}".encode()).hexdigest()[:16]

    def test_stable_id_from_item_id(self) -> None:
        item = _make_item(item_id="99001")
        doc = normalize_item(item, "12345")
        assert doc.id == self._expected_id("99001")

    def test_id_is_16_hex_chars(self) -> None:
        doc = normalize_item(_make_item(item_id="1"), "b1")
        assert len(doc.id) == 16
        assert all(c in "0123456789abcdef" for c in doc.id)

    def test_id_different_for_different_items(self) -> None:
        doc1 = normalize_item(_make_item(item_id="111"), "b1")
        doc2 = normalize_item(_make_item(item_id="222"), "b1")
        assert doc1.id != doc2.id

    def test_id_stable_across_calls(self) -> None:
        item = _make_item(item_id="55")
        assert normalize_item(item, "b1").id == normalize_item(item, "b1").id

    def test_type_is_work_item(self) -> None:
        doc = normalize_item(_make_item(), "b1")
        assert doc.type == "work_item"

    def test_source_is_monday_com(self) -> None:
        doc = normalize_item(_make_item(), "b1")
        assert doc.source == "monday_com"

    def test_title_is_item_name(self) -> None:
        doc = normalize_item(_make_item(name="Fix bug #42"), "b1")
        assert "Fix bug #42" in doc.title

    def test_content_includes_column_values(self) -> None:
        item = _make_item(column_values=[{"id": "status", "text": "Done"}])
        doc = normalize_item(item, "b1")
        assert "Done" in doc.content
        assert "status" in doc.content

    def test_content_includes_board_id(self) -> None:
        doc = normalize_item(_make_item(), "BOARD_99")
        assert "BOARD_99" in doc.content

    def test_metadata_has_required_fields(self) -> None:
        item = _make_item(item_id="42", name="Task A")
        doc = normalize_item(item, "100")
        assert doc.metadata["item_id"] == "42"
        assert doc.metadata["item_name"] == "Task A"
        assert doc.metadata["board_id"] == "100"

    def test_empty_column_values(self) -> None:
        item = _make_item(column_values=[])
        doc = normalize_item(item, "b1")
        assert isinstance(doc, ConnectorDocument)

    def test_none_column_values(self) -> None:
        item = {"id": "1", "name": "Task", "column_values": None}
        doc = normalize_item(item, "b1")
        assert isinstance(doc, ConnectorDocument)

    def test_column_value_empty_text_excluded(self) -> None:
        item = _make_item(column_values=[{"id": "status", "text": ""}])
        doc = normalize_item(item, "b1")
        # empty-text column should not appear as "status: "
        assert "status: " not in doc.content


# ─────────────────────────────────────────────────────────────────────────────
# 5. with_retry
# ─────────────────────────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        calls: List[int] = []

        async def fn() -> str:
            calls.append(1)
            return "ok"

        result = await with_retry(fn, max_attempts=3)
        assert result == "ok"
        assert len(calls) == 1

    async def test_retries_on_monday_com_error(self) -> None:
        calls: List[int] = []

        async def fn() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise MondayComError("transient")
            return "recovered"

        result = await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert result == "recovered"
        assert len(calls) == 3

    async def test_no_retry_on_auth_error(self) -> None:
        calls: List[int] = []

        async def fn() -> str:
            calls.append(1)
            raise MondayComAuthError("bad token")

        with pytest.raises(MondayComAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert len(calls) == 1

    async def test_raises_after_max_attempts(self) -> None:
        async def fn() -> str:
            raise MondayComNetworkError("timeout")

        with pytest.raises(MondayComNetworkError):
            await with_retry(fn, max_attempts=2, base_delay=0.01)

    async def test_works_with_sync_callable(self) -> None:
        def fn() -> str:
            return "sync_ok"

        result = await with_retry(fn, max_attempts=2)
        assert result == "sync_ok"

    async def test_retries_on_network_error(self) -> None:
        calls: List[int] = []

        async def fn() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise MondayComNetworkError("timeout")
            return "ok"

        result = await with_retry(fn, max_attempts=2, base_delay=0.01)
        assert result == "ok"

    async def test_retries_on_rate_limit(self) -> None:
        calls: List[int] = []

        async def fn() -> str:
            calls.append(1)
            if len(calls) < 2:
                raise MondayComRateLimitError("rate limited")
            return "ok"

        result = await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert result == "ok"

    async def test_raises_last_exception_type(self) -> None:
        async def fn() -> str:
            raise MondayComNetworkError("net")

        with pytest.raises(MondayComNetworkError):
            await with_retry(fn, max_attempts=3, base_delay=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# 6. MondayComHTTPClient — _raise_for_status
# ─────────────────────────────────────────────────────────────────────────────

class TestRaiseForStatus:
    def _client(self) -> MondayComHTTPClient:
        return MondayComHTTPClient()

    def test_401_raises_auth_error(self) -> None:
        with pytest.raises(MondayComAuthError):
            self._client()._raise_for_status(401, "ctx")

    def test_403_raises_auth_error(self) -> None:
        with pytest.raises(MondayComAuthError):
            self._client()._raise_for_status(403, "ctx")

    def test_429_raises_rate_limit_error(self) -> None:
        with pytest.raises(MondayComRateLimitError):
            self._client()._raise_for_status(429, "ctx")

    def test_500_raises_network_error(self) -> None:
        with pytest.raises(MondayComNetworkError):
            self._client()._raise_for_status(500, "ctx")

    def test_503_raises_network_error(self) -> None:
        with pytest.raises(MondayComNetworkError):
            self._client()._raise_for_status(503, "ctx")

    def test_404_raises_monday_com_error(self) -> None:
        with pytest.raises(MondayComError):
            self._client()._raise_for_status(404, "ctx")

    def test_400_raises_monday_com_error(self) -> None:
        with pytest.raises(MondayComError):
            self._client()._raise_for_status(400, "ctx")

    def test_200_does_not_raise(self) -> None:
        self._client()._raise_for_status(200, "ctx")  # no exception

    def test_201_does_not_raise(self) -> None:
        self._client()._raise_for_status(201, "ctx")  # no exception


# ─────────────────────────────────────────────────────────────────────────────
# 7. MondayComHTTPClient — _check_graphql_errors
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckGraphQLErrors:
    def _client(self) -> MondayComHTTPClient:
        return MondayComHTTPClient()

    def test_empty_list_does_not_raise(self) -> None:
        self._client()._check_graphql_errors([], "ctx")

    def test_auth_error_on_not_authenticated(self) -> None:
        with pytest.raises(MondayComAuthError):
            self._client()._check_graphql_errors(
                [{"message": "Not authenticated"}], "ctx"
            )

    def test_auth_error_on_invalid_api_key(self) -> None:
        with pytest.raises(MondayComAuthError):
            self._client()._check_graphql_errors(
                [{"message": "Invalid API key"}], "ctx"
            )

    def test_auth_error_on_unauthorized(self) -> None:
        with pytest.raises(MondayComAuthError):
            self._client()._check_graphql_errors(
                [{"message": "Unauthorized access"}], "ctx"
            )

    def test_rate_limit_error(self) -> None:
        with pytest.raises(MondayComRateLimitError):
            self._client()._check_graphql_errors(
                [{"message": "Rate limit exceeded"}], "ctx"
            )

    def test_complexity_budget_rate_limit(self) -> None:
        with pytest.raises(MondayComRateLimitError):
            self._client()._check_graphql_errors(
                [{"message": "Complexity budget exhausted"}], "ctx"
            )

    def test_not_found_error(self) -> None:
        with pytest.raises(MondayComNotFoundError):
            self._client()._check_graphql_errors(
                [{"message": "Resource not found"}], "ctx"
            )

    def test_generic_error(self) -> None:
        with pytest.raises(MondayComError):
            self._client()._check_graphql_errors(
                [{"message": "Something went wrong unexpectedly"}], "ctx"
            )

    def test_error_message_in_exception(self) -> None:
        with pytest.raises(MondayComAuthError, match="not authenticated"):
            self._client()._check_graphql_errors(
                [{"message": "Not authenticated. Use a valid API key."}], "ctx"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 8. MondayComHTTPClient — execute_query
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteQuery:
    def _client(self) -> MondayComHTTPClient:
        return MondayComHTTPClient()

    async def test_returns_data_on_success(self) -> None:
        client = self._client()
        body = {"data": {"me": {"name": "Alice"}}}
        mock_session = _make_aiohttp_mock(status=200, body=body)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            data = await client.execute_query(API_KEY, "{ me { name } }", context="test")
        assert data == {"me": {"name": "Alice"}}

    async def test_raises_auth_error_on_401(self) -> None:
        client = self._client()
        with patch(
            "client.http_client.aiohttp.ClientSession",
            return_value=_make_aiohttp_mock(status=401),
        ):
            with pytest.raises(MondayComAuthError):
                await client.execute_query(API_KEY, "{ me { name } }")

    async def test_raises_auth_error_on_403(self) -> None:
        client = self._client()
        with patch(
            "client.http_client.aiohttp.ClientSession",
            return_value=_make_aiohttp_mock(status=403),
        ):
            with pytest.raises(MondayComAuthError):
                await client.execute_query(API_KEY, "{ me { name } }")

    async def test_raises_rate_limit_error_on_429(self) -> None:
        client = self._client()
        with patch(
            "client.http_client.aiohttp.ClientSession",
            return_value=_make_aiohttp_mock(status=429),
        ):
            with pytest.raises(MondayComRateLimitError):
                await client.execute_query(API_KEY, "{ me { name } }")

    async def test_raises_network_error_on_500(self) -> None:
        client = self._client()
        with patch(
            "client.http_client.aiohttp.ClientSession",
            return_value=_make_aiohttp_mock(status=500),
        ):
            with pytest.raises(MondayComNetworkError):
                await client.execute_query(API_KEY, "{ me { name } }")

    async def test_raises_monday_com_error_on_graphql_errors(self) -> None:
        client = self._client()
        body = {"errors": [{"message": "Some GraphQL error"}], "data": None}
        with patch(
            "client.http_client.aiohttp.ClientSession",
            return_value=_make_aiohttp_mock(status=200, body=body),
        ):
            with pytest.raises(MondayComError):
                await client.execute_query(API_KEY, "{ me { name } }")

    async def test_raises_monday_com_error_on_null_data(self) -> None:
        client = self._client()
        body = {"data": None}
        with patch(
            "client.http_client.aiohttp.ClientSession",
            return_value=_make_aiohttp_mock(status=200, body=body),
        ):
            with pytest.raises(MondayComError):
                await client.execute_query(API_KEY, "{ me { name } }")

    async def test_raises_network_error_on_client_error(self) -> None:
        import aiohttp as _aiohttp

        client = self._client()
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(side_effect=_aiohttp.ClientError("refused"))

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MondayComNetworkError):
                await client.execute_query(API_KEY, "{ me { name } }")


# ─────────────────────────────────────────────────────────────────────────────
# 9. MondayComHTTPClient — convenience methods
# ─────────────────────────────────────────────────────────────────────────────

class TestHTTPClientMethods:
    def _client(self) -> MondayComHTTPClient:
        return MondayComHTTPClient()

    async def test_get_me_returns_user(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(
            return_value={"me": {"id": "1", "name": "Alice", "email": "alice@acme.com"}}
        )
        result = await client.get_me(API_KEY)
        assert result["name"] == "Alice"
        assert result["email"] == "alice@acme.com"

    async def test_get_me_returns_empty_on_null(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(return_value={"me": None})
        result = await client.get_me(API_KEY)
        assert result == {}

    async def test_list_boards_returns_list(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(
            return_value={"boards": [_make_board("1"), _make_board("2")]}
        )
        boards = await client.list_boards(API_KEY)
        assert len(boards) == 2

    async def test_list_boards_passes_pagination_vars(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(return_value={"boards": []})
        await client.list_boards(API_KEY, page=3, limit=25)
        call_kwargs = client.execute_query.call_args
        # variables are passed positionally or as keyword
        args, kwargs = call_kwargs
        variables = kwargs.get("variables") or (args[2] if len(args) > 2 else None)
        if variables is None:
            # Try getting from the mock's positional call
            all_args = list(args) + list(kwargs.values())
            variables = next(
                (a for a in all_args if isinstance(a, dict) and "page" in a), {}
            )
        assert variables.get("page") == 3
        assert variables.get("limit") == 25

    async def test_list_boards_returns_empty_on_null(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(return_value={"boards": None})
        result = await client.list_boards(API_KEY)
        assert result == []

    async def test_get_board_returns_board(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(
            return_value={"boards": [_make_board_detailed("12345")]}
        )
        board = await client.get_board(API_KEY, "12345")
        assert board["id"] == "12345"
        assert "groups" in board
        assert "columns" in board

    async def test_get_board_raises_not_found_on_empty(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(return_value={"boards": []})
        with pytest.raises(MondayComNotFoundError):
            await client.get_board(API_KEY, "99999")

    async def test_list_board_items_first_page(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(
            return_value={
                "boards": [
                    {
                        "items_page": {
                            "cursor": "tok_next",
                            "items": [_make_item("1"), _make_item("2")],
                        }
                    }
                ]
            }
        )
        page = await client.list_board_items(API_KEY, "12345", limit=100)
        assert len(page["items"]) == 2
        assert page["cursor"] == "tok_next"

    async def test_list_board_items_next_page_with_cursor(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(
            return_value={
                "next_items_page": {
                    "cursor": None,
                    "items": [_make_item("3")],
                }
            }
        )
        page = await client.list_board_items(
            API_KEY, "12345", limit=100, cursor="prev_cursor"
        )
        assert len(page["items"]) == 1
        assert page["cursor"] is None

    async def test_list_board_items_empty_board(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(return_value={"boards": []})
        page = await client.list_board_items(API_KEY, "99999", limit=10)
        assert page == {}

    async def test_get_item_returns_item(self) -> None:
        client = self._client()
        item = _make_item("99001")
        item["board"] = {"id": "12345", "name": "Marketing"}
        client.execute_query = AsyncMock(return_value={"items": [item]})
        result = await client.get_item(API_KEY, "99001")
        assert result["id"] == "99001"

    async def test_get_item_raises_not_found_on_empty(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(return_value={"items": []})
        with pytest.raises(MondayComNotFoundError):
            await client.get_item(API_KEY, "99999")

    async def test_list_teams_returns_list(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(
            return_value={"teams": [{"id": "t1", "name": "Engineering"}, {"id": "t2", "name": "Design"}]}
        )
        teams = await client.list_teams(API_KEY)
        assert len(teams) == 2
        assert teams[0]["name"] == "Engineering"

    async def test_list_teams_returns_empty_on_null(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(return_value={"teams": None})
        result = await client.list_teams(API_KEY)
        assert result == []

    async def test_list_users_returns_list(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(
            return_value={
                "users": [
                    {"id": "u1", "name": "Alice", "email": "alice@acme.com", "enabled": True},
                    {"id": "u2", "name": "Bob", "email": "bob@acme.com", "enabled": True},
                ]
            }
        )
        users = await client.list_users(API_KEY)
        assert len(users) == 2

    async def test_list_users_returns_empty_on_null(self) -> None:
        client = self._client()
        client.execute_query = AsyncMock(return_value={"users": None})
        result = await client.list_users(API_KEY)
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# 10. MondayComConnector — init and internals
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorInit:
    def test_defaults(self) -> None:
        c = MondayComConnector()
        assert c.tenant_id == ""
        assert c.connector_id == ""
        assert c.config == {}

    def test_with_config(self) -> None:
        c = _make_connector({"api_key": "mytoken"})
        assert c.config["api_key"] == "mytoken"
        assert c.tenant_id == TENANT
        assert c.connector_id == CONNECTOR_ID

    def test_connector_type(self) -> None:
        assert MondayComConnector.CONNECTOR_TYPE == "monday_com"

    def test_auth_type(self) -> None:
        assert MondayComConnector.AUTH_TYPE == "api_key"

    def test_get_api_key(self) -> None:
        c = _make_connector({"api_key": "tok_abc"})
        assert c._get_api_key() == "tok_abc"

    def test_get_api_key_strips_whitespace(self) -> None:
        c = _make_connector({"api_key": "  tok_abc  "})
        assert c._get_api_key() == "tok_abc"

    def test_get_api_key_missing(self) -> None:
        c = MondayComConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={})
        assert c._get_api_key() == ""

    def test_ensure_client_creates_on_demand(self) -> None:
        c = _make_connector()
        assert c._http_client is None
        client = c._ensure_client()
        assert isinstance(client, MondayComHTTPClient)
        assert c._http_client is client

    def test_ensure_client_returns_same_instance(self) -> None:
        c = _make_connector()
        assert c._ensure_client() is c._ensure_client()


# ─────────────────────────────────────────────────────────────────────────────
# 11. MondayComConnector — install
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorInstall:
    async def test_install_ok_with_key(self) -> None:
        c = _make_connector({"api_key": API_KEY})
        result = await c.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    async def test_install_fails_missing_key(self) -> None:
        c = MondayComConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={})
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_fails_empty_key(self) -> None:
        c = MondayComConnector(
            tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"api_key": ""}
        )
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE

    async def test_install_message_present(self) -> None:
        c = _make_connector()
        result = await c.install()
        assert result.message != ""

    async def test_install_returns_install_result(self) -> None:
        result = await _make_connector().install()
        assert isinstance(result, InstallResult)


# ─────────────────────────────────────────────────────────────────────────────
# 12. MondayComConnector — health_check
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorHealthCheck:
    async def test_health_check_ok(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_me=AsyncMock(
                    return_value={"id": "1", "name": "Alice Smith", "email": "alice@acme.com"}
                )
            )
        )
        result = await c.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Alice Smith" in result.message

    async def test_health_check_missing_key(self) -> None:
        c = MondayComConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={})
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_me=AsyncMock(side_effect=MondayComAuthError("invalid token"))
            )
        )
        result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_me=AsyncMock(side_effect=MondayComNetworkError("timeout"))
            )
        )
        result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_falls_back_to_email(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_me=AsyncMock(
                    return_value={"id": "1", "name": "", "email": "user@acme.com"}
                )
            )
        )
        result = await c.health_check()
        assert "user@acme.com" in result.message

    async def test_health_check_returns_health_check_result(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_me=AsyncMock(return_value={"name": "Bob", "email": "bob@test.com"})
            )
        )
        result = await c.health_check()
        assert isinstance(result, HealthCheckResult)


# ─────────────────────────────────────────────────────────────────────────────
# 13. MondayComConnector — list_boards
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorListBoards:
    async def test_list_boards_single_page(self) -> None:
        c = _make_connector()
        boards = [_make_board("1"), _make_board("2")]
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                list_boards=AsyncMock(side_effect=[boards, []])
            )
        )
        result = await c.list_boards(limit=50)
        assert len(result) == 2

    async def test_list_boards_paginates(self) -> None:
        c = _make_connector()
        page1 = [_make_board(str(i)) for i in range(50)]
        page2 = [_make_board(str(i)) for i in range(50, 60)]
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                list_boards=AsyncMock(side_effect=[page1, page2, []])
            )
        )
        result = await c.list_boards(limit=50)
        assert len(result) == 60

    async def test_list_boards_returns_empty(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(list_boards=AsyncMock(return_value=[]))
        )
        result = await c.list_boards()
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# 14. MondayComConnector — get_board
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorGetBoard:
    async def test_get_board_returns_board(self) -> None:
        c = _make_connector()
        board = _make_board_detailed("12345")
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_board=AsyncMock(return_value=board))
        )
        result = await c.get_board("12345")
        assert result["id"] == "12345"
        assert "groups" in result
        assert "columns" in result

    async def test_get_board_propagates_not_found(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_board=AsyncMock(side_effect=MondayComNotFoundError("not found"))
            )
        )
        with pytest.raises(MondayComNotFoundError):
            await c.get_board("99999")


# ─────────────────────────────────────────────────────────────────────────────
# 15. MondayComConnector — list_board_items (cursor pagination)
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorListBoardItems:
    async def test_list_board_items_single_page(self) -> None:
        c = _make_connector()
        items = [_make_item("1"), _make_item("2")]
        page_result = {"cursor": None, "items": items}
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                list_board_items=AsyncMock(return_value=page_result)
            )
        )
        result = await c.list_board_items("12345")
        assert len(result) == 2

    async def test_list_board_items_cursor_pagination(self) -> None:
        c = _make_connector()
        page1 = {"cursor": "cursor_abc", "items": [_make_item(str(i)) for i in range(50)]}
        page2 = {"cursor": None, "items": [_make_item(str(i)) for i in range(50, 70)]}

        call_count = [0]

        async def mock_list(api_key: str, board_id: str, limit: int = 100, cursor: Optional[str] = None):
            call_count[0] += 1
            return page1 if cursor is None else page2

        c._ensure_client = MagicMock(return_value=MagicMock(list_board_items=mock_list))
        result = await c.list_board_items("12345", limit=50)
        assert len(result) == 70
        assert call_count[0] == 2

    async def test_list_board_items_returns_empty(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                list_board_items=AsyncMock(return_value={"cursor": None, "items": []})
            )
        )
        result = await c.list_board_items("12345")
        assert result == []

    async def test_list_board_items_stops_on_empty_cursor(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                list_board_items=AsyncMock(
                    return_value={"cursor": "", "items": [_make_item("1")]}
                )
            )
        )
        result = await c.list_board_items("12345")
        assert len(result) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 16. MondayComConnector — get_item
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorGetItem:
    async def test_get_item_returns_item(self) -> None:
        c = _make_connector()
        item = _make_item("99001")
        item["board"] = {"id": "12345", "name": "Marketing Board"}
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_item=AsyncMock(return_value=item))
        )
        result = await c.get_item("99001")
        assert result["id"] == "99001"
        assert result["name"] == "Design new logo"

    async def test_get_item_propagates_not_found(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_item=AsyncMock(side_effect=MondayComNotFoundError("not found"))
            )
        )
        with pytest.raises(MondayComNotFoundError):
            await c.get_item("99999")


# ─────────────────────────────────────────────────────────────────────────────
# 17. MondayComConnector — list_teams
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorListTeams:
    async def test_list_teams_returns_list(self) -> None:
        c = _make_connector()
        teams = [{"id": "t1", "name": "Engineering"}, {"id": "t2", "name": "Design"}]
        c._ensure_client = MagicMock(
            return_value=MagicMock(list_teams=AsyncMock(return_value=teams))
        )
        result = await c.list_teams()
        assert len(result) == 2
        assert result[0]["name"] == "Engineering"

    async def test_list_teams_returns_empty(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(list_teams=AsyncMock(return_value=[]))
        )
        assert await c.list_teams() == []


# ─────────────────────────────────────────────────────────────────────────────
# 18. MondayComConnector — list_users
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorListUsers:
    async def test_list_users_single_page(self) -> None:
        c = _make_connector()
        users = [
            {"id": "u1", "name": "Alice", "email": "alice@a.com", "enabled": True},
            {"id": "u2", "name": "Bob", "email": "bob@b.com", "enabled": True},
        ]
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                list_users=AsyncMock(side_effect=[users, []])
            )
        )
        result = await c.list_users(limit=50)
        assert len(result) == 2

    async def test_list_users_paginates(self) -> None:
        c = _make_connector()
        page1 = [{"id": str(i), "name": f"User{i}", "email": f"u{i}@a.com", "enabled": True} for i in range(50)]
        page2 = [{"id": str(i), "name": f"User{i}", "email": f"u{i}@a.com", "enabled": True} for i in range(50, 55)]
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                list_users=AsyncMock(side_effect=[page1, page2, []])
            )
        )
        result = await c.list_users(limit=50)
        assert len(result) == 55

    async def test_list_users_returns_empty(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(list_users=AsyncMock(return_value=[]))
        )
        assert await c.list_users() == []


# ─────────────────────────────────────────────────────────────────────────────
# 19. MondayComConnector — sync
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorSync:
    async def test_sync_success(self) -> None:
        c = _make_connector()
        boards = [_make_board("1", "Board A"), _make_board("2", "Board B")]
        items = [_make_item("10", "Task One"), _make_item("11", "Task Two")]

        c.list_boards = AsyncMock(return_value=boards)
        c.list_board_items = AsyncMock(return_value=items)

        result = await c.sync()
        assert isinstance(result, SyncResult)
        assert result.status == SyncStatus.COMPLETED
        # 2 board docs + (2 boards × 2 items) = 6 total
        assert result.documents_found == 6
        assert result.documents_synced == 6
        assert result.documents_failed == 0

    async def test_sync_partial_on_board_failure(self) -> None:
        c = _make_connector()
        boards = [_make_board("1", "Board A"), _make_board("2", "Board B")]

        async def mock_list_items(board_id: str, limit: int = 100):
            if board_id == "1":
                return [_make_item("10")]
            raise MondayComError("board fetch error")

        c.list_boards = AsyncMock(return_value=boards)
        c.list_board_items = mock_list_items

        result = await c.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed == 1

    async def test_sync_failed_on_auth_error(self) -> None:
        c = _make_connector()
        c.list_boards = AsyncMock(side_effect=MondayComAuthError("invalid token"))

        result = await c.sync()
        assert result.status == SyncStatus.FAILED
        assert "invalid token" in result.message

    async def test_sync_empty_boards(self) -> None:
        c = _make_connector()
        c.list_boards = AsyncMock(return_value=[])

        result = await c.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    async def test_sync_skips_board_without_id(self) -> None:
        c = _make_connector()
        boards = [{"id": "", "name": "Empty ID Board"}, _make_board("1")]
        c.list_boards = AsyncMock(return_value=boards)
        c.list_board_items = AsyncMock(return_value=[_make_item("10")])

        result = await c.sync()
        # Only 1 board doc + 1 item doc = 2
        assert result.documents_found == 2

    async def test_sync_message_contains_board_count(self) -> None:
        c = _make_connector()
        boards = [_make_board("1")]
        c.list_boards = AsyncMock(return_value=boards)
        c.list_board_items = AsyncMock(return_value=[_make_item("10"), _make_item("11")])

        result = await c.sync()
        assert "board" in result.message.lower()

    async def test_sync_failed_on_generic_error(self) -> None:
        c = _make_connector()
        c.list_boards = AsyncMock(side_effect=MondayComError("network fail"))

        result = await c.sync()
        assert result.status == SyncStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 20. MondayComConnector — lifecycle (aclose / context manager)
# ─────────────────────────────────────────────────────────────────────────────

class TestMondayComConnectorLifecycle:
    async def test_aclose_clears_client(self) -> None:
        c = _make_connector()
        c._http_client = MondayComHTTPClient()
        await c.aclose()
        assert c._http_client is None

    async def test_context_manager(self) -> None:
        async with MondayComConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY},
        ) as c:
            assert c.tenant_id == TENANT
        assert c._http_client is None
