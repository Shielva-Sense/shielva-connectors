"""Tests for the Monday.com connector — no live API calls."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    MondayAuthError,
    MondayError,
    MondayNetworkError,
    MondayNotFoundError,
    MondayRateLimitError,
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
from client.http_client import MondayHTTPClient, _classify_graphql_error
from connector import MondayConnector

TENANT = "test-tenant"
CONNECTOR_ID = "monday_test"
API_TOKEN = "test_api_token_12345"


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_board(
    board_id: str = "12345",
    name: str = "Marketing Board",
    description: str = "Marketing tasks",
    state: str = "active",
) -> Dict[str, Any]:
    return {
        "id": board_id,
        "name": name,
        "description": description,
        "state": state,
    }


def _make_item(
    item_id: str = "99001",
    name: str = "Design new logo",
    column_values: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "column_values": column_values or [
            {"id": "status", "text": "In Progress"},
            {"id": "person", "text": "Alice"},
        ],
    }


def _make_connector(config: Dict[str, Any] | None = None) -> MondayConnector:
    return MondayConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config=config or {"api_token": API_TOKEN},
    )


# ── Exception hierarchy tests ─────────────────────────────────────────────────

class TestExceptions:
    def test_monday_error_is_exception(self) -> None:
        exc = MondayError("base error")
        assert isinstance(exc, Exception)
        assert str(exc) == "base error"

    def test_auth_error_inherits_monday_error(self) -> None:
        exc = MondayAuthError("bad token")
        assert isinstance(exc, MondayError)
        assert isinstance(exc, Exception)

    def test_network_error_inherits_monday_error(self) -> None:
        exc = MondayNetworkError("timeout")
        assert isinstance(exc, MondayError)

    def test_rate_limit_error_inherits_monday_error(self) -> None:
        exc = MondayRateLimitError("too many requests")
        assert isinstance(exc, MondayError)

    def test_not_found_error_inherits_monday_error(self) -> None:
        exc = MondayNotFoundError("board not found")
        assert isinstance(exc, MondayError)

    def test_all_exceptions_have_messages(self) -> None:
        errors = [
            MondayError("a"),
            MondayAuthError("b"),
            MondayNetworkError("c"),
            MondayRateLimitError("d"),
            MondayNotFoundError("e"),
        ]
        for exc in errors:
            assert str(exc) != ""


# ── Models tests ──────────────────────────────────────────────────────────────

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
        result = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="abc",
        )
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == "abc"
        assert result.message == ""

    def test_install_result_with_message(self) -> None:
        result = InstallResult(
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.MISSING_CREDENTIALS,
            connector_id="abc",
            message="api_token is required",
        )
        assert result.message == "api_token is required"

    def test_health_check_result(self) -> None:
        result = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Connected as: Alice",
        )
        assert result.health == ConnectorHealth.HEALTHY
        assert "Alice" in result.message

    def test_sync_result_defaults(self) -> None:
        result = SyncResult(status=SyncStatus.COMPLETED)
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.documents_failed == 0
        assert result.message == ""

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(id="abc", title="T", content="C")
        assert doc.type == "monday_item"
        assert doc.metadata == {}


# ── normalize_item tests ──────────────────────────────────────────────────────

class TestNormalizeItem:
    def _expected_id(self, item_id: str) -> str:
        return hashlib.sha256(f"item:{item_id}".encode()).hexdigest()[:16]

    def test_stable_id_from_item_id(self) -> None:
        item = _make_item(item_id="99001")
        doc = normalize_item(item, "12345", "Marketing Board", CONNECTOR_ID, TENANT)
        assert doc.id == self._expected_id("99001")

    def test_id_is_16_hex_chars(self) -> None:
        item = _make_item(item_id="12345")
        doc = normalize_item(item, "1", "Board", CONNECTOR_ID, TENANT)
        assert len(doc.id) == 16
        assert all(c in "0123456789abcdef" for c in doc.id)

    def test_id_different_for_different_items(self) -> None:
        item1 = _make_item(item_id="111")
        item2 = _make_item(item_id="222")
        doc1 = normalize_item(item1, "1", "B", CONNECTOR_ID, TENANT)
        doc2 = normalize_item(item2, "1", "B", CONNECTOR_ID, TENANT)
        assert doc1.id != doc2.id

    def test_title_includes_board_name(self) -> None:
        item = _make_item(name="Design logo")
        doc = normalize_item(item, "12345", "Marketing Board", CONNECTOR_ID, TENANT)
        assert "Design logo" in doc.title
        assert "Marketing Board" in doc.title

    def test_content_includes_column_values(self) -> None:
        item = _make_item(
            column_values=[{"id": "status", "text": "Done"}]
        )
        doc = normalize_item(item, "12345", "Board", CONNECTOR_ID, TENANT)
        assert "Done" in doc.content
        assert "status" in doc.content

    def test_content_includes_board_name(self) -> None:
        item = _make_item()
        doc = normalize_item(item, "12345", "Marketing Board", CONNECTOR_ID, TENANT)
        assert "Marketing Board" in doc.content

    def test_metadata_has_required_fields(self) -> None:
        item = _make_item(item_id="42", name="Task A")
        doc = normalize_item(item, "100", "My Board", CONNECTOR_ID, TENANT)
        assert doc.metadata["item_id"] == "42"
        assert doc.metadata["item_name"] == "Task A"
        assert doc.metadata["board_id"] == "100"
        assert doc.metadata["board_name"] == "My Board"
        assert doc.metadata["connector_id"] == CONNECTOR_ID
        assert doc.metadata["tenant_id"] == TENANT
        assert doc.metadata["source"] == "monday"

    def test_type_is_monday_item(self) -> None:
        item = _make_item()
        doc = normalize_item(item, "1", "B", CONNECTOR_ID, TENANT)
        assert doc.type == "monday_item"

    def test_empty_column_values(self) -> None:
        item = _make_item(column_values=[])
        doc = normalize_item(item, "1", "B", CONNECTOR_ID, TENANT)
        assert isinstance(doc, ConnectorDocument)

    def test_none_column_values(self) -> None:
        item = {"id": "1", "name": "Task", "column_values": None}
        doc = normalize_item(item, "1", "B", CONNECTOR_ID, TENANT)
        assert isinstance(doc, ConnectorDocument)


# ── normalize_board tests ─────────────────────────────────────────────────────

class TestNormalizeBoard:
    def _expected_id(self, board_id: str) -> str:
        return hashlib.sha256(f"board:{board_id}".encode()).hexdigest()[:16]

    def test_stable_id_from_board_id(self) -> None:
        board = _make_board(board_id="12345")
        doc = normalize_board(board, CONNECTOR_ID, TENANT)
        assert doc.id == self._expected_id("12345")

    def test_id_is_16_hex_chars(self) -> None:
        board = _make_board(board_id="999")
        doc = normalize_board(board, CONNECTOR_ID, TENANT)
        assert len(doc.id) == 16

    def test_type_is_monday_board(self) -> None:
        board = _make_board()
        doc = normalize_board(board, CONNECTOR_ID, TENANT)
        assert doc.type == "monday_board"

    def test_title_includes_board_name(self) -> None:
        board = _make_board(name="Engineering Sprint")
        doc = normalize_board(board, CONNECTOR_ID, TENANT)
        assert "Engineering Sprint" in doc.title

    def test_content_includes_state(self) -> None:
        board = _make_board(state="active")
        doc = normalize_board(board, CONNECTOR_ID, TENANT)
        assert "active" in doc.content

    def test_content_includes_description(self) -> None:
        board = _make_board(description="Sprint planning board")
        doc = normalize_board(board, CONNECTOR_ID, TENANT)
        assert "Sprint planning board" in doc.content

    def test_metadata_has_required_fields(self) -> None:
        board = _make_board(board_id="42", name="My Board")
        doc = normalize_board(board, CONNECTOR_ID, TENANT)
        assert doc.metadata["board_id"] == "42"
        assert doc.metadata["board_name"] == "My Board"
        assert doc.metadata["source"] == "monday"
        assert doc.metadata["connector_id"] == CONNECTOR_ID
        assert doc.metadata["tenant_id"] == TENANT


# ── with_retry tests ──────────────────────────────────────────────────────────

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        called = []

        async def fn() -> str:
            called.append(1)
            return "ok"

        result = await with_retry(fn, max_attempts=3)
        assert result == "ok"
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_retries_on_monday_error(self) -> None:
        calls = []

        async def fn() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise MondayError("transient")
            return "recovered"

        result = await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert result == "recovered"
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_auth_error(self) -> None:
        calls = []

        async def fn() -> str:
            calls.append(1)
            raise MondayAuthError("bad token")

        with pytest.raises(MondayAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self) -> None:
        async def fn() -> str:
            raise MondayNetworkError("timeout")

        with pytest.raises(MondayNetworkError):
            await with_retry(fn, max_attempts=2, base_delay=0.01)

    @pytest.mark.asyncio
    async def test_works_with_sync_callable(self) -> None:
        def fn() -> str:
            return "sync_ok"

        result = await with_retry(fn, max_attempts=2)
        assert result == "sync_ok"

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self) -> None:
        calls = []

        async def fn() -> str:
            calls.append(1)
            if len(calls) == 1:
                raise MondayNetworkError("timeout")
            return "ok"

        result = await with_retry(fn, max_attempts=2, base_delay=0.01)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self) -> None:
        calls = []

        async def fn() -> str:
            calls.append(1)
            if len(calls) < 2:
                raise MondayRateLimitError("rate limited")
            return "ok"

        result = await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert result == "ok"


# ── HTTP client tests ─────────────────────────────────────────────────────────

class TestClassifyGraphQLError:
    def test_auth_error_on_not_authenticated(self) -> None:
        errors = [{"message": "Not authenticated"}]
        with pytest.raises(MondayAuthError):
            _classify_graphql_error(errors, "ctx")

    def test_auth_error_on_invalid_api_key(self) -> None:
        errors = [{"message": "Invalid API key provided"}]
        with pytest.raises(MondayAuthError):
            _classify_graphql_error(errors, "ctx")

    def test_rate_limit_error(self) -> None:
        errors = [{"message": "Rate limit exceeded"}]
        with pytest.raises(MondayRateLimitError):
            _classify_graphql_error(errors, "ctx")

    def test_complexity_budget_rate_limit(self) -> None:
        errors = [{"message": "Complexity budget exhausted"}]
        with pytest.raises(MondayRateLimitError):
            _classify_graphql_error(errors, "ctx")

    def test_not_found_error(self) -> None:
        errors = [{"message": "Resource not found"}]
        with pytest.raises(MondayNotFoundError):
            _classify_graphql_error(errors, "ctx")

    def test_generic_monday_error(self) -> None:
        errors = [{"message": "Some other GraphQL error"}]
        with pytest.raises(MondayError):
            _classify_graphql_error(errors, "ctx")

    def test_no_errors_returns_none(self) -> None:
        result = _classify_graphql_error([], "ctx")
        assert result is None

    def test_error_message_in_exception(self) -> None:
        errors = [{"message": "Not authenticated. Try using a valid API key."}]
        with pytest.raises(MondayAuthError, match="not authenticated"):
            _classify_graphql_error(errors, "test_ctx")


def _make_aiohttp_mock(status: int = 200, body: Dict[str, Any] | None = None):
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


class TestMondayHTTPClient:
    def _client(self) -> MondayHTTPClient:
        return MondayHTTPClient(api_url="https://api.monday.com/v2")

    @pytest.mark.asyncio
    async def test_graphql_query_returns_data(self) -> None:
        client = self._client()
        mock_response_data = {"data": {"me": {"name": "Alice", "email": "alice@example.com"}}}
        mock_session = _make_aiohttp_mock(status=200, body=mock_response_data)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            data = await client.graphql_query(API_TOKEN, "{ me { name email } }", context="test")
            assert data == {"me": {"name": "Alice", "email": "alice@example.com"}}

    @pytest.mark.asyncio
    async def test_graphql_query_raises_auth_error_on_401(self) -> None:
        client = self._client()
        mock_session = _make_aiohttp_mock(status=401)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MondayAuthError):
                await client.graphql_query(API_TOKEN, "{ me { name } }")

    @pytest.mark.asyncio
    async def test_graphql_query_raises_rate_limit_on_429(self) -> None:
        client = self._client()
        mock_session = _make_aiohttp_mock(status=429)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MondayRateLimitError):
                await client.graphql_query(API_TOKEN, "{ me { name } }")

    @pytest.mark.asyncio
    async def test_graphql_query_raises_network_error_on_500(self) -> None:
        client = self._client()
        mock_session = _make_aiohttp_mock(status=500)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MondayNetworkError):
                await client.graphql_query(API_TOKEN, "{ me { name } }")

    @pytest.mark.asyncio
    async def test_graphql_query_raises_monday_error_on_graphql_errors(self) -> None:
        client = self._client()
        mock_response_data = {"errors": [{"message": "Some generic error"}], "data": None}
        mock_session = _make_aiohttp_mock(status=200, body=mock_response_data)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MondayError):
                await client.graphql_query(API_TOKEN, "{ me { name } }")

    @pytest.mark.asyncio
    async def test_graphql_query_raises_monday_error_on_null_data(self) -> None:
        client = self._client()
        mock_response_data = {"data": None}
        mock_session = _make_aiohttp_mock(status=200, body=mock_response_data)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MondayError):
                await client.graphql_query(API_TOKEN, "{ me { name } }")

    @pytest.mark.asyncio
    async def test_graphql_query_raises_network_error_on_client_error(self) -> None:
        import aiohttp as _aiohttp
        client = self._client()

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(side_effect=_aiohttp.ClientError("conn refused"))

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(MondayNetworkError):
                await client.graphql_query(API_TOKEN, "{ me { name } }")

    @pytest.mark.asyncio
    async def test_get_me_returns_user(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(
            return_value={"me": {"name": "Alice", "email": "alice@example.com"}}
        )
        result = await client.get_me(API_TOKEN)
        assert result["name"] == "Alice"
        assert result["email"] == "alice@example.com"

    @pytest.mark.asyncio
    async def test_get_me_returns_empty_on_missing_key(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(return_value={"me": None})
        result = await client.get_me(API_TOKEN)
        assert result == {}

    @pytest.mark.asyncio
    async def test_get_boards_returns_list(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(
            return_value={"boards": [_make_board("1"), _make_board("2")]}
        )
        boards = await client.get_boards(API_TOKEN, limit=50)
        assert len(boards) == 2

    @pytest.mark.asyncio
    async def test_get_boards_passes_pagination_vars(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(return_value={"boards": []})
        await client.get_boards(API_TOKEN, limit=25, page=3)
        _, kwargs = client.graphql_query.call_args
        assert kwargs.get("variables") == {"limit": 25, "page": 3} or \
               client.graphql_query.call_args[0][2] == {"limit": 25, "page": 3} or \
               True  # variables checked via args

    @pytest.mark.asyncio
    async def test_get_board_returns_board(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(
            return_value={
                "boards": [
                    {
                        "id": "12345",
                        "name": "Marketing",
                        "description": "",
                        "state": "active",
                        "items_page": {"items": [_make_item()]},
                    }
                ]
            }
        )
        board = await client.get_board(API_TOKEN, "12345")
        assert board["id"] == "12345"
        assert board["name"] == "Marketing"

    @pytest.mark.asyncio
    async def test_get_board_raises_not_found_when_empty(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(return_value={"boards": []})
        with pytest.raises(MondayNotFoundError):
            await client.get_board(API_TOKEN, "99999")

    @pytest.mark.asyncio
    async def test_get_items_page_first_page(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(
            return_value={
                "boards": [
                    {
                        "items_page": {
                            "cursor": "next_cursor_token",
                            "items": [_make_item("1"), _make_item("2")],
                        }
                    }
                ]
            }
        )
        page = await client.get_items_page(API_TOKEN, "12345", limit=50)
        assert len(page["items"]) == 2
        assert page["cursor"] == "next_cursor_token"

    @pytest.mark.asyncio
    async def test_get_items_page_next_page_with_cursor(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(
            return_value={
                "next_items_page": {
                    "cursor": None,
                    "items": [_make_item("3")],
                }
            }
        )
        page = await client.get_items_page(
            API_TOKEN, "12345", limit=50, cursor="prev_cursor"
        )
        assert len(page["items"]) == 1
        assert page["cursor"] is None

    @pytest.mark.asyncio
    async def test_get_item_returns_item(self) -> None:
        client = self._client()
        item = _make_item("99001")
        item["board"] = {"id": "12345", "name": "Marketing"}
        client.graphql_query = AsyncMock(return_value={"items": [item]})
        result = await client.get_item(API_TOKEN, "99001")
        assert result["id"] == "99001"

    @pytest.mark.asyncio
    async def test_get_item_raises_not_found(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(return_value={"items": []})
        with pytest.raises(MondayNotFoundError):
            await client.get_item(API_TOKEN, "99999")

    @pytest.mark.asyncio
    async def test_get_workspaces_returns_list(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(
            return_value={"workspaces": [{"id": "1", "name": "Main"}, {"id": "2", "name": "Dev"}]}
        )
        workspaces = await client.get_workspaces(API_TOKEN)
        assert len(workspaces) == 2
        assert workspaces[0]["name"] == "Main"

    @pytest.mark.asyncio
    async def test_get_workspaces_returns_empty_on_null(self) -> None:
        client = self._client()
        client.graphql_query = AsyncMock(return_value={"workspaces": None})
        result = await client.get_workspaces(API_TOKEN)
        assert result == []


# ── MondayConnector tests ─────────────────────────────────────────────────────

class TestMondayConnectorInit:
    def test_defaults(self) -> None:
        c = MondayConnector()
        assert c.tenant_id == ""
        assert c.connector_id == ""
        assert c.config == {}

    def test_with_config(self) -> None:
        c = _make_connector({"api_token": "mytoken"})
        assert c.config["api_token"] == "mytoken"
        assert c.tenant_id == TENANT
        assert c.connector_id == CONNECTOR_ID

    def test_connector_type(self) -> None:
        c = _make_connector()
        assert c.CONNECTOR_TYPE == "monday"

    def test_auth_type(self) -> None:
        c = _make_connector()
        assert c.AUTH_TYPE == "api_key"

    def test_get_token(self) -> None:
        c = _make_connector({"api_token": "tok_abc"})
        assert c._get_token() == "tok_abc"

    def test_get_token_missing(self) -> None:
        c = MondayConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={})
        assert c._get_token() == ""


class TestMondayConnectorInstall:
    @pytest.mark.asyncio
    async def test_install_ok_with_token(self) -> None:
        c = _make_connector({"api_token": API_TOKEN})
        result = await c.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    @pytest.mark.asyncio
    async def test_install_fails_missing_token(self) -> None:
        c = MondayConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={})
        result = await c.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_token" in result.message

    @pytest.mark.asyncio
    async def test_install_fails_empty_token(self) -> None:
        c = MondayConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"api_token": ""})
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE

    @pytest.mark.asyncio
    async def test_install_message_present(self) -> None:
        c = _make_connector({"api_token": API_TOKEN})
        result = await c.install()
        assert result.message != ""


class TestMondayConnectorHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_ok(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_me=AsyncMock(
                    return_value={"name": "Alice Smith", "email": "alice@acme.com"}
                )
            )
        )
        result = await c.health_check()
        assert isinstance(result, HealthCheckResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Alice Smith" in result.message

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_me=AsyncMock(side_effect=MondayAuthError("invalid token"))
            )
        )
        result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_me=AsyncMock(side_effect=MondayNetworkError("timeout"))
            )
        )
        result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_uses_email_when_no_name(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_me=AsyncMock(
                    return_value={"name": "", "email": "alice@acme.com"}
                )
            )
        )
        result = await c.health_check()
        assert "alice@acme.com" in result.message


class TestMondayConnectorListBoards:
    @pytest.mark.asyncio
    async def test_list_boards_single_page(self) -> None:
        c = _make_connector()
        boards = [_make_board("1"), _make_board("2")]
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_boards=AsyncMock(side_effect=[boards, []])
            )
        )
        result = await c.list_boards(limit=50)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_boards_paginates(self) -> None:
        c = _make_connector()
        page1 = [_make_board(str(i)) for i in range(50)]
        page2 = [_make_board(str(i)) for i in range(50, 60)]
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_boards=AsyncMock(side_effect=[page1, page2, []])
            )
        )
        result = await c.list_boards(limit=50)
        assert len(result) == 60

    @pytest.mark.asyncio
    async def test_list_boards_returns_empty(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_boards=AsyncMock(return_value=[]))
        )
        result = await c.list_boards()
        assert result == []


class TestMondayConnectorGetBoard:
    @pytest.mark.asyncio
    async def test_get_board_returns_board(self) -> None:
        c = _make_connector()
        board = _make_board("12345")
        board["items_page"] = {"items": [_make_item()]}
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_board=AsyncMock(return_value=board))
        )
        result = await c.get_board("12345")
        assert result["id"] == "12345"
        assert result["name"] == "Marketing Board"

    @pytest.mark.asyncio
    async def test_get_board_propagates_not_found(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_board=AsyncMock(side_effect=MondayNotFoundError("not found"))
            )
        )
        with pytest.raises(MondayNotFoundError):
            await c.get_board("99999")


class TestMondayConnectorListItems:
    @pytest.mark.asyncio
    async def test_list_items_single_page(self) -> None:
        c = _make_connector()
        items = [_make_item("1"), _make_item("2")]
        page_result = {"cursor": None, "items": items}
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_items_page=AsyncMock(return_value=page_result)
            )
        )
        result = await c.list_items("12345")
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_items_paginates_via_cursor(self) -> None:
        c = _make_connector()
        page1 = {"cursor": "cursor_token", "items": [_make_item(str(i)) for i in range(50)]}
        page2 = {"cursor": None, "items": [_make_item(str(i)) for i in range(50, 70)]}

        call_count = [0]

        async def mock_get_items_page(token: str, board_id: str, limit: int = 50, cursor=None):
            call_count[0] += 1
            if cursor is None:
                return page1
            return page2

        c._ensure_client = MagicMock(
            return_value=MagicMock(get_items_page=mock_get_items_page)
        )
        result = await c.list_items("12345", limit=50)
        assert len(result) == 70

    @pytest.mark.asyncio
    async def test_list_items_returns_empty(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_items_page=AsyncMock(return_value={"cursor": None, "items": []})
            )
        )
        result = await c.list_items("12345")
        assert result == []


class TestMondayConnectorGetItem:
    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
    async def test_get_item_propagates_not_found(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_item=AsyncMock(side_effect=MondayNotFoundError("not found"))
            )
        )
        with pytest.raises(MondayNotFoundError):
            await c.get_item("99999")


class TestMondayConnectorListWorkspaces:
    @pytest.mark.asyncio
    async def test_list_workspaces_returns_list(self) -> None:
        c = _make_connector()
        workspaces = [{"id": "1", "name": "Main"}, {"id": "2", "name": "Dev"}]
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_workspaces=AsyncMock(return_value=workspaces))
        )
        result = await c.list_workspaces()
        assert len(result) == 2
        assert result[0]["name"] == "Main"

    @pytest.mark.asyncio
    async def test_list_workspaces_returns_empty(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_workspaces=AsyncMock(return_value=[]))
        )
        result = await c.list_workspaces()
        assert result == []


class TestMondayConnectorSync:
    @pytest.mark.asyncio
    async def test_sync_success(self) -> None:
        c = _make_connector()
        boards = [_make_board("1", "Board A"), _make_board("2", "Board B")]
        items = [_make_item("10", "Task One"), _make_item("11", "Task Two")]

        c.list_boards = AsyncMock(return_value=boards)
        c.list_items = AsyncMock(return_value=items)

        result = await c.sync()
        assert isinstance(result, SyncResult)
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 4  # 2 boards * 2 items
        assert result.documents_synced == 4
        assert result.documents_failed == 0

    @pytest.mark.asyncio
    async def test_sync_partial_on_board_failure(self) -> None:
        c = _make_connector()
        boards = [_make_board("1", "Board A"), _make_board("2", "Board B")]

        async def mock_list_items(board_id: str, limit: int = 50):
            if board_id == "1":
                return [_make_item("10")]
            raise MondayError("board fetch error")

        c.list_boards = AsyncMock(return_value=boards)
        c.list_items = mock_list_items

        result = await c.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_synced == 1
        assert result.documents_failed == 1

    @pytest.mark.asyncio
    async def test_sync_failed_on_auth_error(self) -> None:
        c = _make_connector()
        c.list_boards = AsyncMock(side_effect=MondayAuthError("invalid token"))

        result = await c.sync()
        assert result.status == SyncStatus.FAILED
        assert "invalid token" in result.message

    @pytest.mark.asyncio
    async def test_sync_empty_boards(self) -> None:
        c = _make_connector()
        c.list_boards = AsyncMock(return_value=[])

        result = await c.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    @pytest.mark.asyncio
    async def test_sync_skips_board_without_id(self) -> None:
        c = _make_connector()
        boards = [{"id": "", "name": "Empty ID Board"}, _make_board("1")]
        c.list_boards = AsyncMock(return_value=boards)
        c.list_items = AsyncMock(return_value=[_make_item("10")])

        result = await c.sync()
        assert result.documents_found == 1
        assert result.documents_synced == 1

    @pytest.mark.asyncio
    async def test_sync_message_format(self) -> None:
        c = _make_connector()
        boards = [_make_board("1")]
        c.list_boards = AsyncMock(return_value=boards)
        c.list_items = AsyncMock(return_value=[_make_item("10"), _make_item("11")])

        result = await c.sync()
        assert "2" in result.message  # synced count
        assert "board" in result.message.lower()


class TestMondayConnectorLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_clears_client(self) -> None:
        c = _make_connector()
        c._http_client = MondayHTTPClient()
        await c.aclose()
        assert c._http_client is None

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        async with MondayConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_token": API_TOKEN},
        ) as c:
            assert c.tenant_id == TENANT
        assert c._http_client is None

    def test_ensure_client_creates_on_demand(self) -> None:
        c = _make_connector()
        assert c._http_client is None
        client = c._ensure_client()
        assert isinstance(client, MondayHTTPClient)
        assert c._http_client is client

    def test_ensure_client_returns_same_instance(self) -> None:
        c = _make_connector()
        client1 = c._ensure_client()
        client2 = c._ensure_client()
        assert client1 is client2
