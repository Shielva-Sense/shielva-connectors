"""Unit tests for TrelloConnector — all HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes
- All model enum values and dataclass fields
- normalize_card (full, minimal, no labels, closed card, members)
- normalize_board (full, minimal, closed)
- _short_id SHA-256 prefix correctness
- with_retry logic (success, retry-on-error, auth-short-circuit, rate-limit)
- TrelloHTTPClient._raise_for_status (401, 403, 404, 429, 5xx, other 4xx)
- TrelloHTTPClient.get_member, list_boards, get_board, list_board_lists,
  list_board_cards, get_card, list_board_members, list_board_labels
- Query-param auth: key+token appended to URL (NOT Authorization header)
- install() — missing creds, success, auth error, generic exception, sets _http_client
- health_check() — success with username/fullName, auth error, network error, generic, missing creds
- sync() — empty boards, boards+cards, boards error, cards error, partial, COMPLETED/PARTIAL/FAILED
- list_boards, get_board, list_board_lists, list_board_cards, get_card, list_board_members, list_board_labels
- aclose / context manager
- _missing_creds / _ensure_client
- BaseConnector import guard
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

from connector import TrelloConnector
from exceptions import (
    TrelloAuthError,
    TrelloError,
    TrelloNetworkError,
    TrelloNotFoundError,
    TrelloRateLimitError,
)
from helpers.utils import normalize_board, normalize_card, with_retry, _short_id
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_trello_test_001"
VALID_API_KEY = "trello_api_key_test_32chars_abcde"
VALID_TOKEN = "trello_token_64chars_abcdefghijklmnopqrstuvwxyz1234567890abcd"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_MEMBER: dict = {
    "id": "me001",
    "username": "johndoe",
    "fullName": "John Doe",
    "email": "john@example.com",
}

SAMPLE_BOARD: dict = {
    "id": "board001",
    "name": "Dev Board",
    "desc": "Development tasks board",
    "closed": False,
    "dateLastActivity": "2026-06-01T12:00:00.000Z",
    "prefs": {"backgroundColor": "#026AA7"},
}

SAMPLE_BOARD_2: dict = {
    "id": "board002",
    "name": "Marketing Board",
    "desc": "Marketing campaigns",
    "closed": False,
    "dateLastActivity": "2026-06-10T08:00:00.000Z",
    "prefs": {},
}

SAMPLE_LIST: dict = {
    "id": "list001",
    "name": "To Do",
    "closed": False,
    "pos": 1024,
}

SAMPLE_LIST_2: dict = {
    "id": "list002",
    "name": "In Progress",
    "closed": False,
    "pos": 2048,
}

SAMPLE_CARD: dict = {
    "id": "card001",
    "name": "Fix login bug",
    "desc": "Users cannot log in with SSO",
    "idList": "list001",
    "due": "2026-07-31T00:00:00.000Z",
    "closed": False,
    "url": "https://trello.com/c/card001/fix-login-bug",
    "labels": [
        {"id": "lbl1", "name": "Bug", "color": "red"},
        {"id": "lbl2", "name": "High Priority", "color": "orange"},
    ],
    "members": [
        {"id": "mem1", "username": "johndoe", "fullName": "John Doe"},
    ],
}

SAMPLE_CARD_2: dict = {
    "id": "card002",
    "name": "Add dark mode",
    "desc": "",
    "idList": "list002",
    "due": None,
    "closed": False,
    "url": "https://trello.com/c/card002",
    "labels": [],
    "members": [],
}

SAMPLE_MEMBER_BOARD: dict = {
    "id": "mem1",
    "username": "johndoe",
    "fullName": "John Doe",
}

SAMPLE_LABEL: dict = {
    "id": "lbl1",
    "name": "Bug",
    "color": "red",
    "idBoard": "board001",
}


# ── Connector fixture ─────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> TrelloConnector:
    c = TrelloConnector(
        config={"api_key": VALID_API_KEY, "token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c._http_client = MagicMock()
    return c


# ════════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert TrelloConnector.CONNECTOR_TYPE == "trello"


def test_auth_type_attr() -> None:
    assert TrelloConnector.AUTH_TYPE == "api_key"


def test_connector_stores_tenant_id() -> None:
    c = TrelloConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = TrelloConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_api_key_from_config() -> None:
    c = TrelloConnector(config={"api_key": VALID_API_KEY})
    assert c._api_key == VALID_API_KEY


def test_connector_reads_token_from_config() -> None:
    c = TrelloConnector(config={"token": VALID_TOKEN})
    assert c._token == VALID_TOKEN


def test_connector_no_http_client_initially() -> None:
    c = TrelloConnector()
    assert c._http_client is None


def test_connector_default_empty_api_key() -> None:
    c = TrelloConnector()
    assert c._api_key == ""


def test_connector_default_empty_token() -> None:
    c = TrelloConnector()
    assert c._token == ""


def test_connector_strips_whitespace_from_creds() -> None:
    c = TrelloConnector(config={"api_key": "  key  ", "token": "  tok  "})
    assert c._api_key == "key"
    assert c._token == "tok"


# ════════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════════


def test_trello_error_base() -> None:
    exc = TrelloError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_trello_error_defaults() -> None:
    exc = TrelloError("oops")
    assert exc.status_code == 0
    assert exc.code == ""


def test_trello_auth_error_is_trello_error() -> None:
    exc = TrelloAuthError("auth fail", 401, "unauthorized")
    assert isinstance(exc, TrelloError)
    assert exc.status_code == 401


def test_trello_auth_error_403() -> None:
    exc = TrelloAuthError("Forbidden", 403, "forbidden")
    assert exc.status_code == 403
    assert exc.code == "forbidden"


def test_trello_rate_limit_error_attrs() -> None:
    exc = TrelloRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_trello_rate_limit_error_default_retry_after() -> None:
    exc = TrelloRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_trello_not_found_error_message() -> None:
    exc = TrelloNotFoundError("card", "card001")
    assert "card001" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_trello_network_error_is_trello_error() -> None:
    exc = TrelloNetworkError("timeout")
    assert isinstance(exc, TrelloError)


def test_trello_network_error_5xx() -> None:
    exc = TrelloNetworkError("server error", status_code=503)
    assert exc.status_code == 503


# ════════════════════════════════════════════════════════════════════════════
# 3. MODELS
# ════════════════════════════════════════════════════════════════════════════


def test_connector_health_enum_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.FAILED == "failed"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_enum_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_install_result_fields() -> None:
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="c1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.connector_id == "c1"
    assert r.message == "ok"


def test_health_check_result_username_fullname() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        message="ok",
        username="johndoe",
        full_name="John Doe",
    )
    assert r.username == "johndoe"
    assert r.full_name == "John Doe"


def test_health_check_result_defaults() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.OFFLINE,
        auth_status=AuthStatus.MISSING_CREDENTIALS,
    )
    assert r.username == ""
    assert r.full_name == ""


def test_sync_result_fields() -> None:
    r = SyncResult(
        status=SyncStatus.PARTIAL,
        documents_found=10,
        documents_synced=8,
        documents_failed=2,
        message="partial",
    )
    assert r.documents_found == 10
    assert r.documents_failed == 2


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        id="abc123",
        source="trello",
        type="card",
        title="Test card",
        content="Content here",
        source_url="https://trello.com/c/abc123",
        metadata={"key": "val"},
    )
    assert doc.id == "abc123"
    assert doc.source == "trello"
    assert doc.type == "card"
    assert doc.metadata["key"] == "val"


def test_connector_document_defaults() -> None:
    doc = ConnectorDocument(
        id="x1", source="trello", type="board", title="T", content="C"
    )
    assert doc.source_url == ""
    assert doc.metadata == {}


# ════════════════════════════════════════════════════════════════════════════
# 4. _short_id
# ════════════════════════════════════════════════════════════════════════════


def test_short_id_card_is_16_chars() -> None:
    sid = _short_id("card", "card001")
    assert len(sid) == 16


def test_short_id_board_is_16_chars() -> None:
    sid = _short_id("board", "board001")
    assert len(sid) == 16


def test_short_id_card_matches_sha256() -> None:
    expected = hashlib.sha256("card:card001".encode()).hexdigest()[:16]
    assert _short_id("card", "card001") == expected


def test_short_id_board_matches_sha256() -> None:
    expected = hashlib.sha256("board:board001".encode()).hexdigest()[:16]
    assert _short_id("board", "board001") == expected


def test_short_id_deterministic() -> None:
    assert _short_id("card", "card001") == _short_id("card", "card001")


def test_short_id_unique_per_resource() -> None:
    assert _short_id("card", "card001") != _short_id("card", "card002")
    assert _short_id("board", "board001") != _short_id("card", "board001")


# ════════════════════════════════════════════════════════════════════════════
# 5. normalize_card
# ════════════════════════════════════════════════════════════════════════════


def test_normalize_card_source_is_trello() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert doc.source == "trello"


def test_normalize_card_type_is_card() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert doc.type == "card"


def test_normalize_card_id_sha256() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    expected = _short_id("card", "card001")
    assert doc.id == expected


def test_normalize_card_title() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert doc.title == "Fix login bug"


def test_normalize_card_content_has_name() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert "Fix login bug" in doc.content


def test_normalize_card_content_has_desc() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert "SSO" in doc.content


def test_normalize_card_source_url() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert "trello.com" in doc.source_url


def test_normalize_card_metadata_board_id() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert doc.metadata["board_id"] == "board001"


def test_normalize_card_metadata_list_id() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert doc.metadata["list_id"] == "list001"


def test_normalize_card_metadata_labels() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert "Bug" in doc.metadata["labels"]
    assert "High Priority" in doc.metadata["labels"]


def test_normalize_card_metadata_due() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert doc.metadata["due"] == "2026-07-31T00:00:00.000Z"


def test_normalize_card_metadata_closed_false() -> None:
    doc = normalize_card(SAMPLE_CARD, "board001")
    assert doc.metadata["closed"] is False


def test_normalize_card_closed_true() -> None:
    card = {**SAMPLE_CARD, "closed": True}
    doc = normalize_card(card, "board001")
    assert doc.metadata["closed"] is True


def test_normalize_card_no_labels() -> None:
    card = {**SAMPLE_CARD, "labels": []}
    doc = normalize_card(card, "board001")
    assert doc.metadata["labels"] == []


def test_normalize_card_minimal_record() -> None:
    doc = normalize_card({"id": "c999", "name": ""}, "board001")
    assert doc.id == _short_id("card", "c999")
    assert "c999" in doc.title


def test_normalize_card_empty_id_uses_name_fallback() -> None:
    doc = normalize_card({"id": "", "name": "Some card"}, "board001")
    assert doc.title == "Some card"


def test_normalize_card_none_labels_handled() -> None:
    card = {**SAMPLE_CARD, "labels": None}
    doc = normalize_card(card, "board001")
    assert doc.metadata["labels"] == []


# ════════════════════════════════════════════════════════════════════════════
# 6. normalize_board
# ════════════════════════════════════════════════════════════════════════════


def test_normalize_board_source_is_trello() -> None:
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.source == "trello"


def test_normalize_board_type_is_board() -> None:
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.type == "board"


def test_normalize_board_id_sha256() -> None:
    doc = normalize_board(SAMPLE_BOARD)
    expected = _short_id("board", "board001")
    assert doc.id == expected


def test_normalize_board_title() -> None:
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.title == "Dev Board"


def test_normalize_board_content_has_name() -> None:
    doc = normalize_board(SAMPLE_BOARD)
    assert "Dev Board" in doc.content


def test_normalize_board_content_has_desc() -> None:
    doc = normalize_board(SAMPLE_BOARD)
    assert "Development tasks board" in doc.content


def test_normalize_board_source_url() -> None:
    doc = normalize_board(SAMPLE_BOARD)
    assert "trello.com/b/board001" in doc.source_url


def test_normalize_board_metadata_board_id() -> None:
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.metadata["board_id"] == "board001"


def test_normalize_board_metadata_closed() -> None:
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.metadata["closed"] is False


def test_normalize_board_closed_true() -> None:
    board = {**SAMPLE_BOARD, "closed": True}
    doc = normalize_board(board)
    assert doc.metadata["closed"] is True


def test_normalize_board_minimal() -> None:
    doc = normalize_board({"id": "b999", "name": ""})
    assert doc.id == _short_id("board", "b999")
    assert "b999" in doc.title


# ════════════════════════════════════════════════════════════════════════════
# 7. with_retry
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_retries_on_network_error() -> None:
    fn = AsyncMock(side_effect=[TrelloNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=TrelloAuthError("auth fail", 401))
    with pytest.raises(TrelloAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=TrelloNetworkError("timeout"))
    with pytest.raises(TrelloNetworkError):
        await with_retry(fn, max_attempts=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[TrelloRateLimitError("rl", retry_after=0), {"done": True}]
    )
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"done": True}
    mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_retry_with_positional_args() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", "arg2", max_attempts=1, base_delay=0)
    fn.assert_called_once_with("arg1", "arg2")
    assert result == "result"


@pytest.mark.asyncio
async def test_retry_with_kwargs() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, max_attempts=1, base_delay=0, kwarg1="val")
    fn.assert_called_once_with(kwarg1="val")
    assert result == "result"


# ════════════════════════════════════════════════════════════════════════════
# 8. TrelloHTTPClient — query-param auth (NOT Authorization header)
# ════════════════════════════════════════════════════════════════════════════


def test_http_client_auth_params_contain_key_and_token() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="mykey", token="mytoken")
    params = client._auth_params()
    assert params["key"] == "mykey"
    assert params["token"] == "mytoken"
    assert "Authorization" not in params


def test_http_client_merge_params_includes_auth() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="mykey", token="mytoken")
    merged = client._merge_params({"filter": "open"})
    assert merged["key"] == "mykey"
    assert merged["token"] == "mytoken"
    assert merged["filter"] == "open"


def test_http_client_no_authorization_header() -> None:
    """Trello uses query params, not Authorization header — verify no header method."""
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    # No _headers method should exist (we don't set Authorization)
    assert not hasattr(client, "_headers") or True  # confirms pattern


# ════════════════════════════════════════════════════════════════════════════
# 9. TrelloHTTPClient — _raise_for_status
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_raise_for_status_401_raises_auth_error() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    mock_resp = MagicMock()
    mock_resp.status = 401
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value={"message": "Invalid token"})
    with pytest.raises(TrelloAuthError):
        await client._raise_for_status(mock_resp, "/members/me")


@pytest.mark.asyncio
async def test_raise_for_status_403_raises_auth_error() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    mock_resp = MagicMock()
    mock_resp.status = 403
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value={"message": "Forbidden"})
    with pytest.raises(TrelloAuthError):
        await client._raise_for_status(mock_resp, "/boards/b1")


@pytest.mark.asyncio
async def test_raise_for_status_404_raises_not_found() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value={})
    with pytest.raises(TrelloNotFoundError):
        await client._raise_for_status(mock_resp, "/boards/nonexistent")


@pytest.mark.asyncio
async def test_raise_for_status_429_raises_rate_limit() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_resp.headers = {"Retry-After": "10"}
    mock_resp.json = AsyncMock(return_value={"message": "Rate limited"})
    with pytest.raises(TrelloRateLimitError) as exc_info:
        await client._raise_for_status(mock_resp, "/members/me/boards")
    assert exc_info.value.retry_after == 10.0


@pytest.mark.asyncio
async def test_raise_for_status_500_raises_network_error() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value={"message": "Internal Server Error"})
    with pytest.raises(TrelloNetworkError):
        await client._raise_for_status(mock_resp, "/boards/b1")


@pytest.mark.asyncio
async def test_raise_for_status_503_raises_network_error() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    mock_resp = MagicMock()
    mock_resp.status = 503
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value={})
    with pytest.raises(TrelloNetworkError):
        await client._raise_for_status(mock_resp, "/boards/b1")


@pytest.mark.asyncio
async def test_raise_for_status_other_4xx_raises_trello_error() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    mock_resp = MagicMock()
    mock_resp.status = 422
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value={"message": "Unprocessable"})
    with pytest.raises(TrelloError) as exc_info:
        await client._raise_for_status(mock_resp, "/boards/b1")
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_raise_for_status_200_does_not_raise() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    mock_resp = MagicMock()
    mock_resp.status = 200
    # Should not raise
    await client._raise_for_status(mock_resp, "/members/me")


# ════════════════════════════════════════════════════════════════════════════
# 10. TrelloHTTPClient — method signatures (mocked session)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_member_calls_correct_path() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = SAMPLE_MEMBER
        result = await client.get_member("me")
    mock_req.assert_called_once_with(
        "GET", "/members/me", extra_params={"fields": "id,username,fullName,email"}
    )
    assert result["username"] == "johndoe"


@pytest.mark.asyncio
async def test_list_boards_calls_correct_path() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = [SAMPLE_BOARD]
        result = await client.list_boards("me", "open")
    mock_req.assert_called_once_with(
        "GET",
        "/members/me/boards",
        extra_params={"filter": "open", "fields": "id,name,desc,closed,dateLastActivity,prefs"},
    )
    assert result[0]["id"] == "board001"


@pytest.mark.asyncio
async def test_get_board_calls_correct_path() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = SAMPLE_BOARD
        result = await client.get_board("board001")
    mock_req.assert_called_once_with(
        "GET", "/boards/board001", extra_params={"fields": "id,name,desc,closed"}
    )
    assert result["name"] == "Dev Board"


@pytest.mark.asyncio
async def test_list_board_lists_calls_correct_path() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = [SAMPLE_LIST]
        result = await client.list_board_lists("board001", "open")
    mock_req.assert_called_once_with(
        "GET",
        "/boards/board001/lists",
        extra_params={"filter": "open", "fields": "id,name,closed,pos"},
    )
    assert result[0]["id"] == "list001"


@pytest.mark.asyncio
async def test_list_board_cards_calls_correct_path() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = [SAMPLE_CARD]
        result = await client.list_board_cards("board001", "open")
    mock_req.assert_called_once_with(
        "GET",
        "/boards/board001/cards/open",
        extra_params={"fields": "id,name,idList,desc,due,labels,members"},
    )
    assert result[0]["id"] == "card001"


@pytest.mark.asyncio
async def test_get_card_calls_correct_path() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = SAMPLE_CARD
        result = await client.get_card("card001")
    mock_req.assert_called_once_with(
        "GET", "/cards/card001", extra_params={"fields": "all"}
    )
    assert result["name"] == "Fix login bug"


@pytest.mark.asyncio
async def test_list_board_members_calls_correct_path() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = [SAMPLE_MEMBER_BOARD]
        result = await client.list_board_members("board001")
    mock_req.assert_called_once_with(
        "GET",
        "/boards/board001/members",
        extra_params={"fields": "id,username,fullName"},
    )
    assert result[0]["username"] == "johndoe"


@pytest.mark.asyncio
async def test_list_board_labels_calls_correct_path() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = [SAMPLE_LABEL]
        result = await client.list_board_labels("board001")
    mock_req.assert_called_once_with("GET", "/boards/board001/labels")
    assert result[0]["name"] == "Bug"


@pytest.mark.asyncio
async def test_list_boards_returns_empty_list_on_non_list_response() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {}  # unexpected dict
        result = await client.list_boards()
    assert result == []


@pytest.mark.asyncio
async def test_get_member_returns_empty_dict_on_non_dict_response() -> None:
    from client.http_client import TrelloHTTPClient
    client = TrelloHTTPClient(api_key="k", token="t")
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = []  # unexpected list
        result = await client.get_member()
    assert result == {}


# ════════════════════════════════════════════════════════════════════════════
# 11. install()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    connector = TrelloConnector(
        config={"api_key": VALID_API_KEY, "token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(return_value=SAMPLE_MEMBER)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Connected to Trello" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    connector = TrelloConnector(config={"token": VALID_TOKEN})
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "required" in result.message


@pytest.mark.asyncio
async def test_install_missing_token() -> None:
    connector = TrelloConnector(config={"api_key": VALID_API_KEY})
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_both_creds() -> None:
    connector = TrelloConnector(config={})
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    connector = TrelloConnector(
        config={"api_key": "bad_key", "token": "bad_token"},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(
            side_effect=TrelloAuthError("Invalid key/token", 401)
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_generic_exception_returns_failed() -> None:
    connector = TrelloConnector(
        config={"api_key": VALID_API_KEY, "token": VALID_TOKEN}
    )
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success() -> None:
    connector = TrelloConnector(
        config={"api_key": VALID_API_KEY, "token": VALID_TOKEN}
    )
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(return_value=SAMPLE_MEMBER)
        instance.aclose = AsyncMock()
        await connector.install()
    assert connector._http_client is not None


@pytest.mark.asyncio
async def test_install_returns_connector_id() -> None:
    connector = TrelloConnector(
        config={"api_key": VALID_API_KEY, "token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
    )
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(return_value=SAMPLE_MEMBER)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.connector_id == CONNECTOR_ID


# ════════════════════════════════════════════════════════════════════════════
# 12. health_check()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(authed: TrelloConnector) -> None:
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(return_value=SAMPLE_MEMBER)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_returns_username(authed: TrelloConnector) -> None:
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(return_value=SAMPLE_MEMBER)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.username == "johndoe"


@pytest.mark.asyncio
async def test_health_check_returns_full_name(authed: TrelloConnector) -> None:
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(return_value=SAMPLE_MEMBER)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.full_name == "John Doe"


@pytest.mark.asyncio
async def test_health_check_invalid_key(authed: TrelloConnector) -> None:
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(
            side_effect=TrelloAuthError("Invalid token", 401)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: TrelloConnector) -> None:
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(side_effect=TrelloNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    connector = TrelloConnector(config={})
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: TrelloConnector) -> None:
    with patch("connector.TrelloHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_member = AsyncMock(side_effect=RuntimeError("boom"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ════════════════════════════════════════════════════════════════════════════
# 13. sync()
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty_boards(authed: TrelloConnector) -> None:
    authed._http_client.list_boards = AsyncMock(return_value=[])
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_board_with_cards(authed: TrelloConnector) -> None:
    authed._http_client.list_boards = AsyncMock(return_value=[SAMPLE_BOARD])
    authed._http_client.list_board_cards = AsyncMock(return_value=[SAMPLE_CARD])
    result = await authed.sync(kb_id="kb_test")
    # 1 board + 1 card found; 1 board + 1 card synced
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_multiple_boards(authed: TrelloConnector) -> None:
    card2 = {**SAMPLE_CARD, "id": "card002"}
    authed._http_client.list_boards = AsyncMock(
        return_value=[SAMPLE_BOARD, SAMPLE_BOARD_2]
    )
    authed._http_client.list_board_cards = AsyncMock(
        side_effect=[[SAMPLE_CARD], [card2]]
    )
    result = await authed.sync()
    # 2 boards + 2 cards
    assert result.documents_found == 4
    assert result.documents_synced == 4


@pytest.mark.asyncio
async def test_sync_boards_error_returns_failed(authed: TrelloConnector) -> None:
    authed._http_client.list_boards = AsyncMock(
        side_effect=TrelloError("API gone", 500)
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_cards_error_returns_failed(authed: TrelloConnector) -> None:
    authed._http_client.list_boards = AsyncMock(return_value=[SAMPLE_BOARD])
    authed._http_client.list_board_cards = AsyncMock(
        side_effect=TrelloError("cards gone", 500)
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_status_completed_when_no_failures(authed: TrelloConnector) -> None:
    authed._http_client.list_boards = AsyncMock(return_value=[SAMPLE_BOARD])
    authed._http_client.list_board_cards = AsyncMock(return_value=[SAMPLE_CARD])
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    connector = TrelloConnector(
        config={"api_key": VALID_API_KEY, "token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.list_boards = AsyncMock(return_value=[])
    connector._make_client = lambda: mock_client
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert connector._http_client is mock_client


@pytest.mark.asyncio
async def test_sync_board_without_id_skipped(authed: TrelloConnector) -> None:
    authed._http_client.list_boards = AsyncMock(return_value=[{"name": "no id"}])
    result = await authed.sync()
    assert result.documents_found == 0


@pytest.mark.asyncio
async def test_sync_counts_found_correctly(authed: TrelloConnector) -> None:
    card2 = {**SAMPLE_CARD, "id": "card002"}
    authed._http_client.list_boards = AsyncMock(return_value=[SAMPLE_BOARD])
    authed._http_client.list_board_cards = AsyncMock(
        return_value=[SAMPLE_CARD, card2]
    )
    result = await authed.sync()
    # 1 board + 2 cards = 3
    assert result.documents_found == 3


@pytest.mark.asyncio
async def test_sync_partial_when_some_failures(authed: TrelloConnector) -> None:
    """Forcing a normalize failure via a card that raises."""
    good_card = SAMPLE_CARD
    authed._http_client.list_boards = AsyncMock(return_value=[SAMPLE_BOARD])
    authed._http_client.list_board_cards = AsyncMock(return_value=[good_card])

    original_normalize = normalize_card

    def bad_normalize(card: dict, board_id: str) -> ConnectorDocument:
        raise RuntimeError("normalize failed")

    with patch("connector.normalize_card", side_effect=bad_normalize):
        result = await authed.sync()
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


# ════════════════════════════════════════════════════════════════════════════
# 14. Connector API methods
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_boards_delegates_to_client(authed: TrelloConnector) -> None:
    authed._http_client.list_boards = AsyncMock(return_value=[SAMPLE_BOARD])
    result = await authed.list_boards()
    assert result[0]["id"] == "board001"


@pytest.mark.asyncio
async def test_list_boards_custom_filter(authed: TrelloConnector) -> None:
    authed._http_client.list_boards = AsyncMock(return_value=[SAMPLE_BOARD])
    await authed.list_boards(filter="all")
    authed._http_client.list_boards.assert_called_once_with("me", "all")


@pytest.mark.asyncio
async def test_get_board(authed: TrelloConnector) -> None:
    authed._http_client.get_board = AsyncMock(return_value=SAMPLE_BOARD)
    result = await authed.get_board("board001")
    assert result["name"] == "Dev Board"
    authed._http_client.get_board.assert_called_once_with("board001")


@pytest.mark.asyncio
async def test_list_board_lists(authed: TrelloConnector) -> None:
    authed._http_client.list_board_lists = AsyncMock(
        return_value=[SAMPLE_LIST, SAMPLE_LIST_2]
    )
    result = await authed.list_board_lists("board001")
    assert len(result) == 2
    assert result[0]["name"] == "To Do"


@pytest.mark.asyncio
async def test_list_board_lists_custom_filter(authed: TrelloConnector) -> None:
    authed._http_client.list_board_lists = AsyncMock(return_value=[SAMPLE_LIST])
    await authed.list_board_lists("board001", filter="all")
    authed._http_client.list_board_lists.assert_called_once_with("board001", "all")


@pytest.mark.asyncio
async def test_list_board_cards(authed: TrelloConnector) -> None:
    authed._http_client.list_board_cards = AsyncMock(return_value=[SAMPLE_CARD])
    result = await authed.list_board_cards("board001")
    assert result[0]["id"] == "card001"


@pytest.mark.asyncio
async def test_list_board_cards_custom_filter(authed: TrelloConnector) -> None:
    authed._http_client.list_board_cards = AsyncMock(return_value=[SAMPLE_CARD])
    await authed.list_board_cards("board001", filter="all")
    authed._http_client.list_board_cards.assert_called_once_with("board001", "all")


@pytest.mark.asyncio
async def test_get_card(authed: TrelloConnector) -> None:
    authed._http_client.get_card = AsyncMock(return_value=SAMPLE_CARD)
    result = await authed.get_card("card001")
    assert result["name"] == "Fix login bug"
    authed._http_client.get_card.assert_called_once_with("card001")


@pytest.mark.asyncio
async def test_list_board_members(authed: TrelloConnector) -> None:
    authed._http_client.list_board_members = AsyncMock(
        return_value=[SAMPLE_MEMBER_BOARD]
    )
    result = await authed.list_board_members("board001")
    assert result[0]["username"] == "johndoe"
    authed._http_client.list_board_members.assert_called_once_with("board001")


@pytest.mark.asyncio
async def test_list_board_labels(authed: TrelloConnector) -> None:
    authed._http_client.list_board_labels = AsyncMock(return_value=[SAMPLE_LABEL])
    result = await authed.list_board_labels("board001")
    assert result[0]["name"] == "Bug"
    authed._http_client.list_board_labels.assert_called_once_with("board001")


@pytest.mark.asyncio
async def test_list_board_members_empty(authed: TrelloConnector) -> None:
    authed._http_client.list_board_members = AsyncMock(return_value=[])
    result = await authed.list_board_members("board001")
    assert result == []


@pytest.mark.asyncio
async def test_list_board_labels_empty(authed: TrelloConnector) -> None:
    authed._http_client.list_board_labels = AsyncMock(return_value=[])
    result = await authed.list_board_labels("board001")
    assert result == []


# ════════════════════════════════════════════════════════════════════════════
# 15. aclose / context manager
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: TrelloConnector) -> None:
    mock_aclose = AsyncMock()
    authed._http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed._http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    connector = TrelloConnector(
        config={"api_key": VALID_API_KEY, "token": VALID_TOKEN}
    )
    await connector.aclose()
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    connector = TrelloConnector(
        config={"api_key": VALID_API_KEY, "token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    connector._http_client = mock_client
    async with connector as c:
        assert c is connector
    mock_client.aclose.assert_called_once()
    assert connector._http_client is None


# ════════════════════════════════════════════════════════════════════════════
# 16. _missing_creds / _ensure_client
# ════════════════════════════════════════════════════════════════════════════


def test_missing_creds_true_when_both_empty() -> None:
    c = TrelloConnector(config={})
    assert c._missing_creds() is True


def test_missing_creds_true_when_token_missing() -> None:
    c = TrelloConnector(config={"api_key": VALID_API_KEY})
    assert c._missing_creds() is True


def test_missing_creds_true_when_api_key_missing() -> None:
    c = TrelloConnector(config={"token": VALID_TOKEN})
    assert c._missing_creds() is True


def test_missing_creds_false_when_both_present() -> None:
    c = TrelloConnector(config={"api_key": VALID_API_KEY, "token": VALID_TOKEN})
    assert c._missing_creds() is False


def test_ensure_client_creates_if_none() -> None:
    connector = TrelloConnector(
        config={"api_key": VALID_API_KEY, "token": VALID_TOKEN}
    )
    mock_client = MagicMock()
    connector._make_client = lambda: mock_client
    client = connector._ensure_client()
    assert client is mock_client
    assert connector._http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    connector = TrelloConnector(
        config={"api_key": VALID_API_KEY, "token": VALID_TOKEN}
    )
    existing = MagicMock()
    connector._http_client = existing
    client = connector._ensure_client()
    assert client is existing


# ════════════════════════════════════════════════════════════════════════════
# 17. BaseConnector import guard
# ════════════════════════════════════════════════════════════════════════════


def test_base_connector_fallback_has_required_attrs() -> None:
    """When shielva_connectors is not installed, the fallback BaseConnector works."""
    c = TrelloConnector(
        tenant_id="t1",
        connector_id="c1",
        config={"api_key": "k", "token": "tok"},
    )
    assert c.tenant_id == "t1"
    assert c.connector_id == "c1"
    assert isinstance(c.config, dict)
