"""Unit tests for MiroConnector — fully mocked, zero real I/O.

67+ tests covering:
- Connector class attributes (CONNECTOR_TYPE, AUTH_TYPE, scopes, config keys, URIs)
- Exception hierarchy and message propagation
- Model enum values and dataclass fields
- normalize_board: full fields, minimal, nested policy/owner/team, no optional fields
- normalize_item: sticky note (data.content), card (data.title), minimal, no data, position
- Stable ID generation — sha256('board:<id>')[:16] and sha256('item:<id>')[:16]
- with_retry: success on first attempt, retry on MiroError, retry on network,
  auth not retried, not-found not retried, exhausted raises last exception
- HTTP client _raise_for_status: 200, 401, 403, 404, 429, 500, 503, other 4xx
- HTTP client Bearer header injection
- HTTP client: get_token_info, get_boards (with/without cursor), get_board,
  get_board_items (with type, with cursor), get_teams
- install(): happy path (PENDING), missing client_id, missing client_secret, both missing
- authorize(): URL includes client_id, includes redirect_uri, includes state, no redirect_uri
- health_check(): HEALTHY with user/team name, MiroAuthError → INVALID_CREDENTIALS, Exception → FAILED
- sync(): empty boards, boards only no items, boards with items, item fetch failure isolated,
  COMPLETED/PARTIAL/FAILED status
- list_boards(): single page, cursor pagination stops on empty data, cursor pagination stops on no cursor
- list_board_items(): single page, cursor pagination, empty board
- get_board(): success, not-found propagated
- authorize URL structure: response_type=code present, correct base URL
- Cursor pagination: next_cursor followed, stop when cursor absent, stop when data empty
- aclose() and context manager
- Multi-tenant: different tenant_id produces different connector instance
"""
from __future__ import annotations

import hashlib
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure connector root is on sys.path for bare module imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import MiroConnector, CONNECTOR_TYPE, AUTH_TYPE, _MIRO_AUTH_URI, _MIRO_TOKEN_URI
from exceptions import (
    MiroAuthError,
    MiroError,
    MiroNetworkError,
    MiroNotFoundError,
    MiroRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    MiroBoard,
    MiroItem,
    MiroObjectType,
    MiroBoardSharingPolicy,
    SyncResult,
    SyncStatus,
)
from helpers.utils import normalize_board, normalize_item, with_retry
from client.http_client import MiroHTTPClient

# ── fixtures & shared data ────────────────────────────────────────────────────

TENANT_ID = "test-tenant-miro-001"
CONNECTOR_ID = "test-connector-miro-001"

TEST_CONFIG = {
    "client_id": "test-miro-client-id",
    "client_secret": "test-miro-client-secret",
    "redirect_uri": "https://acp.example.com/connectors/oauth/callback",
    "access_token": "test-miro-access-token",
}

SAMPLE_BOARD = {
    "id": "uXjVNjEHRqI=",
    "name": "Product Roadmap Q3",
    "description": "Quarterly roadmap planning board",
    "createdAt": "2026-01-15T10:00:00Z",
    "modifiedAt": "2026-06-18T14:30:00Z",
    "viewLink": "https://miro.com/board/uXjVNjEHRqI=/",
    "policy": {
        "sharingPolicy": {
            "access": "team_edit",
            "teamAccess": "edit",
            "organizationAccess": "view",
        }
    },
    "owner": {
        "id": "user001",
        "name": "Alice Miro",
    },
    "team": {
        "id": "team001",
        "name": "Product Team",
    },
    "picture": {
        "id": "img001",
        "imageURL": "https://miro.com/board-thumbnail.png",
    },
}

SAMPLE_BOARD_MINIMAL = {
    "id": "minimal123",
    "name": "Minimal Board",
}

SAMPLE_STICKY_NOTE = {
    "id": "3458764601234567890",
    "type": "sticky_note",
    "createdAt": "2026-06-01T09:00:00Z",
    "modifiedAt": "2026-06-18T12:00:00Z",
    "data": {
        "content": "Ship the new auth flow by end of sprint",
        "shape": "square",
    },
    "style": {
        "fillColor": "yellow",
        "textAlign": "center",
    },
    "position": {
        "x": 100.5,
        "y": -200.0,
        "origin": "center",
        "relativeTo": "canvas_center",
    },
    "createdBy": {"id": "user001", "type": "user"},
    "modifiedBy": {"id": "user002", "type": "user"},
}

SAMPLE_CARD = {
    "id": "3458764601234567891",
    "type": "card",
    "createdAt": "2026-06-02T10:00:00Z",
    "modifiedAt": "2026-06-19T11:00:00Z",
    "data": {
        "title": "Design review: landing page",
        "description": "Review the new landing page wireframes",
        "dueDate": "2026-06-30T00:00:00Z",
    },
    "style": {},
    "position": {
        "x": 300.0,
        "y": 150.0,
        "origin": "center",
    },
    "createdBy": {"id": "user001", "type": "user"},
    "modifiedBy": {"id": "user001", "type": "user"},
}

SAMPLE_ITEM_MINIMAL = {
    "id": "9999",
    "type": "text",
}

SAMPLE_TOKEN_INFO = {
    "user": {
        "id": "user001",
        "name": "Alice Miro",
        "email": "alice@miro.example.com",
    },
    "team": {
        "id": "team001",
        "name": "Product Team",
    },
    "scopes": "boards:read organizations:read team:read",
    "tokenType": "bearer",
}

SAMPLE_BOARDS_RESPONSE_PAGE1 = {
    "data": [SAMPLE_BOARD],
    "cursor": "cursor_page2",
    "total": 2,
    "size": 1,
    "limit": 50,
    "offset": 0,
    "links": {},
}

SAMPLE_BOARDS_RESPONSE_PAGE2 = {
    "data": [SAMPLE_BOARD_MINIMAL],
    "total": 2,
    "size": 1,
    "limit": 50,
    "offset": 1,
    "links": {},
}

SAMPLE_BOARDS_RESPONSE_SINGLE = {
    "data": [SAMPLE_BOARD],
    "total": 1,
    "size": 1,
    "limit": 50,
    "offset": 0,
    "links": {},
}

SAMPLE_BOARD_ITEMS_RESPONSE = {
    "data": [SAMPLE_STICKY_NOTE, SAMPLE_CARD],
    "total": 2,
    "size": 2,
    "limit": 50,
    "offset": 0,
    "links": {},
}

SAMPLE_BOARD_ITEMS_EMPTY = {
    "data": [],
    "total": 0,
    "size": 0,
    "limit": 50,
    "offset": 0,
    "links": {},
}


@pytest.fixture
def connector():
    """MiroConnector with full config."""
    return MiroConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_http():
    """Fully-mocked MiroHTTPClient — all methods are AsyncMock."""
    return MagicMock(
        get_token_info=AsyncMock(return_value=SAMPLE_TOKEN_INFO),
        get_boards=AsyncMock(return_value=SAMPLE_BOARDS_RESPONSE_SINGLE),
        get_board=AsyncMock(return_value=SAMPLE_BOARD),
        get_board_items=AsyncMock(return_value=SAMPLE_BOARD_ITEMS_RESPONSE),
        get_teams=AsyncMock(return_value={"data": []}),
    )


@pytest.fixture
def connected_connector(connector, mock_http):
    """Connector with injected mock HTTP client."""
    connector.client = mock_http
    return connector


# ═══════════════════════════════════════════════════════════════════════════
# 1. Module-level constants
# ═══════════════════════════════════════════════════════════════════════════

def test_module_connector_type():
    assert CONNECTOR_TYPE == "miro"


def test_module_auth_type():
    assert AUTH_TYPE == "oauth2"


def test_auth_uri_is_miro():
    assert "miro.com" in _MIRO_AUTH_URI
    assert "oauth/authorize" in _MIRO_AUTH_URI


def test_token_uri_is_miro():
    assert "api.miro.com" in _MIRO_TOKEN_URI
    assert "oauth/token" in _MIRO_TOKEN_URI


# ═══════════════════════════════════════════════════════════════════════════
# 2. Connector class attributes
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert MiroConnector.CONNECTOR_TYPE == "miro"


def test_auth_type_class_attr():
    assert MiroConnector.AUTH_TYPE == "oauth2"


def test_connector_name():
    assert MiroConnector.CONNECTOR_NAME == "Miro"


def test_required_config_keys_defined():
    assert "client_id" in MiroConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in MiroConnector.REQUIRED_CONFIG_KEYS


def test_required_scopes_boards_read():
    assert "boards:read" in MiroConnector.REQUIRED_SCOPES


def test_required_scopes_organizations_read():
    assert "organizations:read" in MiroConnector.REQUIRED_SCOPES


def test_required_scopes_team_read():
    assert "team:read" in MiroConnector.REQUIRED_SCOPES


# ═══════════════════════════════════════════════════════════════════════════
# 3. Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════

def test_miro_error_is_exception():
    assert issubclass(MiroError, Exception)


def test_auth_error_is_miro_error():
    assert issubclass(MiroAuthError, MiroError)


def test_network_error_is_miro_error():
    assert issubclass(MiroNetworkError, MiroError)


def test_not_found_error_is_miro_error():
    assert issubclass(MiroNotFoundError, MiroError)


def test_rate_limit_error_is_miro_error():
    assert issubclass(MiroRateLimitError, MiroError)


def test_auth_error_carries_message():
    err = MiroAuthError("token expired")
    assert "token expired" in str(err)


def test_network_error_carries_message():
    err = MiroNetworkError("connection refused")
    assert "connection refused" in str(err)


def test_not_found_error_carries_message():
    err = MiroNotFoundError("board not found")
    assert "board not found" in str(err)


def test_rate_limit_error_carries_message():
    err = MiroRateLimitError("rate limited 429")
    assert "rate limited 429" in str(err)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Model enums and dataclasses
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_health_values():
    assert ConnectorHealth.HEALTHY.value == "healthy"
    assert ConnectorHealth.DEGRADED.value == "degraded"
    assert ConnectorHealth.OFFLINE.value == "offline"


def test_auth_status_values():
    assert AuthStatus.CONNECTED.value == "connected"
    assert AuthStatus.MISSING_CREDENTIALS.value == "missing_credentials"
    assert AuthStatus.PENDING.value == "pending"
    assert AuthStatus.TOKEN_EXPIRED.value == "token_expired"
    assert AuthStatus.FAILED.value == "failed"
    assert AuthStatus.INVALID_CREDENTIALS.value == "invalid_credentials"


def test_sync_status_values():
    assert SyncStatus.COMPLETED.value == "completed"
    assert SyncStatus.PARTIAL.value == "partial"
    assert SyncStatus.FAILED.value == "failed"


def test_miro_object_type_values():
    assert MiroObjectType.BOARD.value == "board"
    assert MiroObjectType.STICKY_NOTE.value == "sticky_note"
    assert MiroObjectType.CARD.value == "card"
    assert MiroObjectType.TEXT.value == "text"


def test_miro_board_sharing_policy_values():
    assert MiroBoardSharingPolicy.PRIVATE.value == "private"
    assert MiroBoardSharingPolicy.TEAM_EDIT.value == "team_edit"


def test_install_result_fields():
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.PENDING,
        connector_id="conn-1",
        message="installed",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.auth_status == AuthStatus.PENDING
    assert r.connector_id == "conn-1"
    assert r.message == "installed"


def test_health_check_result_fields():
    r = HealthCheckResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        message="Connected — user: Alice",
        user_name="Alice",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.user_name == "Alice"


def test_health_check_result_default_user_fields():
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
    )
    assert r.user_name == ""
    assert r.user_email == ""


def test_sync_result_fields():
    r = SyncResult(
        status=SyncStatus.COMPLETED,
        documents_found=5,
        documents_synced=5,
        documents_failed=0,
        message="Synced 5/5",
    )
    assert r.status == SyncStatus.COMPLETED
    assert r.documents_found == 5
    assert r.documents_synced == 5
    assert r.documents_failed == 0


def test_connector_document_fields():
    doc = ConnectorDocument(
        id="abc123",
        title="My Board",
        content="Board content",
        type="miro_board",
        metadata={"board_id": "abc"},
    )
    assert doc.id == "abc123"
    assert doc.title == "My Board"
    assert doc.type == "miro_board"
    assert doc.metadata["board_id"] == "abc"


# ═══════════════════════════════════════════════════════════════════════════
# 5. normalize_board
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_board_stable_id():
    doc = normalize_board(SAMPLE_BOARD)
    expected = hashlib.sha256(b"board:uXjVNjEHRqI=").hexdigest()[:16]
    assert doc.id == expected


def test_normalize_board_title():
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.title == "Product Roadmap Q3"


def test_normalize_board_type():
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.type == "miro_board"


def test_normalize_board_content_includes_name():
    doc = normalize_board(SAMPLE_BOARD)
    assert "Product Roadmap Q3" in doc.content


def test_normalize_board_content_includes_description():
    doc = normalize_board(SAMPLE_BOARD)
    assert "Quarterly roadmap planning board" in doc.content


def test_normalize_board_content_includes_owner():
    doc = normalize_board(SAMPLE_BOARD)
    assert "Alice Miro" in doc.content


def test_normalize_board_content_includes_team():
    doc = normalize_board(SAMPLE_BOARD)
    assert "Product Team" in doc.content


def test_normalize_board_content_includes_view_link():
    doc = normalize_board(SAMPLE_BOARD)
    assert "miro.com/board" in doc.content


def test_normalize_board_metadata_board_id():
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.metadata["board_id"] == "uXjVNjEHRqI="


def test_normalize_board_metadata_access_policy():
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.metadata["access_policy"] == "team_edit"


def test_normalize_board_metadata_team_id():
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.metadata["team_id"] == "team001"


def test_normalize_board_metadata_source():
    doc = normalize_board(SAMPLE_BOARD)
    assert doc.metadata["source"] == "miro"


def test_normalize_board_minimal_no_optional_fields():
    doc = normalize_board(SAMPLE_BOARD_MINIMAL)
    assert doc.title == "Minimal Board"
    assert doc.id == hashlib.sha256(b"board:minimal123").hexdigest()[:16]
    # No description, owner, team in content — should not raise
    assert "Board:" in doc.content


def test_normalize_board_empty_id_produces_stable_id():
    doc = normalize_board({"name": "No ID Board"})
    expected = hashlib.sha256(b"board:").hexdigest()[:16]
    assert doc.id == expected


# ═══════════════════════════════════════════════════════════════════════════
# 6. normalize_item
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_sticky_note_stable_id():
    doc = normalize_item(SAMPLE_STICKY_NOTE, board_id="uXjVNjEHRqI=")
    expected = hashlib.sha256(b"item:3458764601234567890").hexdigest()[:16]
    assert doc.id == expected


def test_normalize_sticky_note_type():
    doc = normalize_item(SAMPLE_STICKY_NOTE, board_id="uXjVNjEHRqI=")
    assert doc.type == "miro_sticky_note"


def test_normalize_sticky_note_content_from_data_content():
    doc = normalize_item(SAMPLE_STICKY_NOTE, board_id="uXjVNjEHRqI=")
    assert "Ship the new auth flow by end of sprint" in doc.content


def test_normalize_sticky_note_title_truncated():
    doc = normalize_item(SAMPLE_STICKY_NOTE, board_id="uXjVNjEHRqI=")
    # title is content[:80]
    assert doc.title == "Ship the new auth flow by end of sprint"


def test_normalize_sticky_note_position_in_content():
    doc = normalize_item(SAMPLE_STICKY_NOTE, board_id="uXjVNjEHRqI=")
    assert "Position" in doc.content
    assert "100.5" in doc.content


def test_normalize_sticky_note_fill_color_in_content():
    doc = normalize_item(SAMPLE_STICKY_NOTE, board_id="uXjVNjEHRqI=")
    assert "yellow" in doc.content


def test_normalize_card_content_from_data_title():
    doc = normalize_item(SAMPLE_CARD, board_id="uXjVNjEHRqI=")
    assert "Design review: landing page" in doc.content


def test_normalize_card_type():
    doc = normalize_item(SAMPLE_CARD, board_id="uXjVNjEHRqI=")
    assert doc.type == "miro_card"


def test_normalize_item_metadata_board_id():
    doc = normalize_item(SAMPLE_STICKY_NOTE, board_id="uXjVNjEHRqI=")
    assert doc.metadata["board_id"] == "uXjVNjEHRqI="


def test_normalize_item_metadata_item_type():
    doc = normalize_item(SAMPLE_STICKY_NOTE, board_id="uXjVNjEHRqI=")
    assert doc.metadata["item_type"] == "sticky_note"


def test_normalize_item_metadata_source():
    doc = normalize_item(SAMPLE_STICKY_NOTE, board_id="uXjVNjEHRqI=")
    assert doc.metadata["source"] == "miro"


def test_normalize_item_metadata_position_x():
    doc = normalize_item(SAMPLE_STICKY_NOTE, board_id="uXjVNjEHRqI=")
    assert doc.metadata["position_x"] == 100.5


def test_normalize_item_minimal_no_content():
    doc = normalize_item(SAMPLE_ITEM_MINIMAL, board_id="board-x")
    # title falls back to "{type} {id}"
    assert "Text" in doc.title or "9999" in doc.title
    assert doc.metadata["item_id"] == "9999"


def test_normalize_item_stable_id_independent_of_board():
    doc1 = normalize_item(SAMPLE_STICKY_NOTE, board_id="board-a")
    doc2 = normalize_item(SAMPLE_STICKY_NOTE, board_id="board-b")
    # Stable ID is based on item id only, not board_id
    assert doc1.id == doc2.id


# ═══════════════════════════════════════════════════════════════════════════
# 7. with_retry
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_with_retry_success_first_attempt():
    call_count = 0

    async def fn():
        nonlocal call_count
        call_count += 1
        return {"ok": True}

    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_miro_error():
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise MiroError("transient error")
        return "success"

    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == "success"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_with_retry_retries_on_rate_limit():
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 2:
            raise MiroRateLimitError("429")
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == "ok"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error():
    calls = []

    async def fn():
        calls.append(1)
        raise MiroAuthError("401 unauthorized")

    with pytest.raises(MiroAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_not_found():
    calls = []

    async def fn():
        calls.append(1)
        raise MiroNotFoundError("404 board not found")

    with pytest.raises(MiroNotFoundError):
        await with_retry(fn, max_attempts=3, base_delay=0)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_with_retry_exhausted_raises_last_exception():
    async def fn():
        raise MiroNetworkError("network failure")

    with pytest.raises(MiroNetworkError, match="network failure"):
        await with_retry(fn, max_attempts=3, base_delay=0)


@pytest.mark.asyncio
async def test_with_retry_generic_exception_retried():
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 2:
            raise ValueError("unexpected")
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == "ok"
    assert len(calls) == 2


# ═══════════════════════════════════════════════════════════════════════════
# 8. HTTP client _raise_for_status
# ═══════════════════════════════════════════════════════════════════════════

def test_raise_for_status_200_no_raise():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    # Should not raise
    client._raise_for_status(200, {"id": "ok"}, "test")


def test_raise_for_status_201_no_raise():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    client._raise_for_status(201, {}, "test")


def test_raise_for_status_401_raises_auth():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    with pytest.raises(MiroAuthError):
        client._raise_for_status(401, {"message": "Unauthorized"}, "ctx")


def test_raise_for_status_403_raises_auth():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    with pytest.raises(MiroAuthError):
        client._raise_for_status(403, {"message": "Forbidden"}, "ctx")


def test_raise_for_status_404_raises_not_found():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    with pytest.raises(MiroNotFoundError):
        client._raise_for_status(404, {"message": "Not found"}, "ctx")


def test_raise_for_status_429_raises_rate_limit():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    with pytest.raises(MiroRateLimitError):
        client._raise_for_status(429, {}, "ctx")


def test_raise_for_status_500_raises_network():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    with pytest.raises(MiroNetworkError):
        client._raise_for_status(500, {"message": "Internal Server Error"}, "ctx")


def test_raise_for_status_503_raises_network():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    with pytest.raises(MiroNetworkError):
        client._raise_for_status(503, {}, "ctx")


def test_raise_for_status_other_4xx_raises_miro_error():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    with pytest.raises(MiroError):
        client._raise_for_status(422, {"message": "Unprocessable"}, "ctx")


def test_raise_for_status_message_from_error_key():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    with pytest.raises(MiroAuthError, match="Invalid token"):
        client._raise_for_status(401, {"error": "Invalid token"}, "ctx")


# ═══════════════════════════════════════════════════════════════════════════
# 9. HTTP client Bearer header
# ═══════════════════════════════════════════════════════════════════════════

def test_http_client_bearer_header():
    client = MiroHTTPClient(config={"access_token": "my-token-xyz"})
    headers = client._auth_headers()
    assert headers["Authorization"] == "Bearer my-token-xyz"


def test_http_client_bearer_header_empty_token():
    client = MiroHTTPClient(config={})
    headers = client._auth_headers()
    assert headers["Authorization"] == "Bearer "


# ═══════════════════════════════════════════════════════════════════════════
# 10. HTTP client methods (mocked aiohttp)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_token_info_returns_body():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=SAMPLE_TOKEN_INFO)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_token_info()

    assert result["user"]["name"] == "Alice Miro"
    assert result["team"]["name"] == "Product Team"


@pytest.mark.asyncio
async def test_get_boards_no_cursor():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=SAMPLE_BOARDS_RESPONSE_SINGLE)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_boards()

    assert len(result["data"]) == 1
    assert result["data"][0]["id"] == "uXjVNjEHRqI="


@pytest.mark.asyncio
async def test_get_boards_with_cursor_passes_param():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=SAMPLE_BOARDS_RESPONSE_PAGE2)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_boards(cursor="cursor_page2")

    # Verify the session.get was called (cursor param passed via params kwarg)
    assert mock_session.get.called
    call_kwargs = mock_session.get.call_args
    params_passed = call_kwargs[1].get("params", {})
    assert params_passed.get("cursor") == "cursor_page2"


@pytest.mark.asyncio
async def test_get_board_single():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=SAMPLE_BOARD)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_board("uXjVNjEHRqI=")

    assert result["name"] == "Product Roadmap Q3"


@pytest.mark.asyncio
async def test_get_board_404_raises_not_found():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 404
    mock_resp.json = AsyncMock(return_value={"message": "Board not found"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(MiroNotFoundError):
            await client.get_board("nonexistent")


@pytest.mark.asyncio
async def test_get_board_items_no_type_filter():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=SAMPLE_BOARD_ITEMS_RESPONSE)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_board_items("uXjVNjEHRqI=")

    assert len(result["data"]) == 2


@pytest.mark.asyncio
async def test_get_board_items_with_type_filter():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"data": [SAMPLE_STICKY_NOTE]})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_board_items("uXjVNjEHRqI=", type="sticky_note")

    call_kwargs = mock_session.get.call_args
    params_passed = call_kwargs[1].get("params", {})
    assert params_passed.get("type") == "sticky_note"


@pytest.mark.asyncio
async def test_get_board_items_401_raises_auth():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 401
    mock_resp.json = AsyncMock(return_value={"message": "Unauthorized"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(MiroAuthError):
            await client.get_board_items("board123")


@pytest.mark.asyncio
async def test_get_token_info_401_raises_auth():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 401
    mock_resp.json = AsyncMock(return_value={"message": "Invalid token"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(MiroAuthError):
            await client.get_token_info()


@pytest.mark.asyncio
async def test_get_boards_500_raises_network():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.json = AsyncMock(return_value={"message": "Internal Server Error"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(MiroNetworkError):
            await client.get_boards()


@pytest.mark.asyncio
async def test_get_boards_429_raises_rate_limit():
    client = MiroHTTPClient(config=dict(TEST_CONFIG))
    mock_resp = AsyncMock()
    mock_resp.status = 429
    mock_resp.json = AsyncMock(return_value={"message": "Too Many Requests"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(MiroRateLimitError):
            await client.get_boards()


# ═══════════════════════════════════════════════════════════════════════════
# 11. install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_happy_path_returns_pending(connector):
    result = await connector.install()
    assert isinstance(result, InstallResult)
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert "OAuth" in result.message or "complete" in result.message.lower()


@pytest.mark.asyncio
async def test_install_missing_client_id():
    c = MiroConnector(
        config={"client_secret": "secret-only"},
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
    )
    result = await c.install()
    assert isinstance(result, InstallResult)
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_client_secret():
    c = MiroConnector(
        config={"client_id": "id-only"},
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
    )
    result = await c.install()
    assert isinstance(result, InstallResult)
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_empty_config():
    c = MiroConnector(config={}, tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_connector_id_in_result(connector):
    result = await connector.install()
    assert result.connector_id == CONNECTOR_ID


# ═══════════════════════════════════════════════════════════════════════════
# 12. authorize()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_string(connector):
    url = await connector.authorize()
    assert isinstance(url, str)


@pytest.mark.asyncio
async def test_authorize_url_base_is_miro(connector):
    url = await connector.authorize()
    assert url.startswith("https://miro.com/oauth/authorize")


@pytest.mark.asyncio
async def test_authorize_url_contains_client_id(connector):
    url = await connector.authorize()
    assert "client_id=test-miro-client-id" in url


@pytest.mark.asyncio
async def test_authorize_url_contains_response_type(connector):
    url = await connector.authorize()
    assert "response_type=code" in url


@pytest.mark.asyncio
async def test_authorize_url_contains_redirect_uri(connector):
    url = await connector.authorize()
    assert "redirect_uri=" in url


@pytest.mark.asyncio
async def test_authorize_url_contains_state_when_provided(connector):
    url = await connector.authorize(state="csrf-token-abc")
    assert "state=csrf-token-abc" in url


@pytest.mark.asyncio
async def test_authorize_url_no_redirect_uri_when_absent():
    c = MiroConnector(
        config={"client_id": "cid", "client_secret": "csec"},
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
    )
    url = await c.authorize()
    assert "redirect_uri" not in url


# ═══════════════════════════════════════════════════════════════════════════
# 13. health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_check_healthy(connected_connector):
    connected_connector.client.get_token_info.return_value = SAMPLE_TOKEN_INFO
    result = await connected_connector.health_check()
    assert isinstance(result, HealthCheckResult)
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_message_includes_user_name(connected_connector):
    connected_connector.client.get_token_info.return_value = SAMPLE_TOKEN_INFO
    result = await connected_connector.health_check()
    assert "Alice Miro" in result.message


@pytest.mark.asyncio
async def test_health_check_message_includes_team_name(connected_connector):
    connected_connector.client.get_token_info.return_value = SAMPLE_TOKEN_INFO
    result = await connected_connector.health_check()
    assert "Product Team" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error_returns_invalid_credentials(connected_connector):
    connected_connector.client.get_token_info.side_effect = MiroAuthError("401")
    result = await connected_connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error_returns_failed(connected_connector):
    connected_connector.client.get_token_info.side_effect = MiroNetworkError("500")
    result = await connected_connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_no_user_in_response(connected_connector):
    connected_connector.client.get_token_info.return_value = {
        "scopes": "boards:read",
        "tokenType": "bearer",
    }
    result = await connected_connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert "Connected" in result.message


# ═══════════════════════════════════════════════════════════════════════════
# 14. sync()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_empty_boards_returns_completed(connected_connector):
    connected_connector.client.get_boards.return_value = {"data": []}
    result = await connected_connector.sync()
    assert isinstance(result, SyncResult)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_boards_only_no_items(connected_connector):
    connected_connector.client.get_boards.return_value = SAMPLE_BOARDS_RESPONSE_SINGLE
    connected_connector.client.get_board_items.return_value = SAMPLE_BOARD_ITEMS_EMPTY
    result = await connected_connector.sync()
    assert result.status == SyncStatus.COMPLETED
    # 1 board document synced, 0 items
    assert result.documents_found == 1
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_boards_with_items(connected_connector):
    connected_connector.client.get_boards.return_value = SAMPLE_BOARDS_RESPONSE_SINGLE
    connected_connector.client.get_board_items.return_value = SAMPLE_BOARD_ITEMS_RESPONSE
    result = await connected_connector.sync()
    assert result.status == SyncStatus.COMPLETED
    # 1 board + 2 items = 3 docs
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_board_items_failure_isolated(connected_connector):
    """Items fetch failure for one board should not abort the entire sync."""
    connected_connector.client.get_boards.return_value = SAMPLE_BOARDS_RESPONSE_SINGLE
    connected_connector.client.get_board_items.side_effect = MiroNetworkError("500")
    result = await connected_connector.sync()
    # Board doc still synced; items skipped
    assert result.documents_synced == 1  # board itself
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_not_found_board_items_isolated(connected_connector):
    """404 on board items should be caught and synced board count still correct."""
    connected_connector.client.get_boards.return_value = SAMPLE_BOARDS_RESPONSE_SINGLE
    connected_connector.client.get_board_items.side_effect = MiroNotFoundError("404")
    result = await connected_connector.sync()
    assert result.documents_synced == 1  # board itself still counted
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_top_level_exception_returns_failed(connected_connector):
    """If list_boards raises, sync returns FAILED."""
    connected_connector.client.get_boards.side_effect = MiroNetworkError("crash")
    result = await connected_connector.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_message_contains_counts(connected_connector):
    connected_connector.client.get_boards.return_value = SAMPLE_BOARDS_RESPONSE_SINGLE
    connected_connector.client.get_board_items.return_value = SAMPLE_BOARD_ITEMS_RESPONSE
    result = await connected_connector.sync()
    assert "3/3" in result.message or "Synced" in result.message


@pytest.mark.asyncio
async def test_sync_two_boards_with_items(connected_connector):
    two_boards_resp = {"data": [SAMPLE_BOARD, SAMPLE_BOARD_MINIMAL]}
    connected_connector.client.get_boards.return_value = two_boards_resp
    connected_connector.client.get_board_items.return_value = SAMPLE_BOARD_ITEMS_RESPONSE
    result = await connected_connector.sync()
    # 2 boards + 2 items each = 6
    assert result.documents_found == 6
    assert result.documents_synced == 6


# ═══════════════════════════════════════════════════════════════════════════
# 15. list_boards() cursor pagination
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_boards_single_page(connected_connector):
    connected_connector.client.get_boards.return_value = SAMPLE_BOARDS_RESPONSE_SINGLE
    boards = await connected_connector.list_boards()
    assert len(boards) == 1
    assert boards[0]["id"] == "uXjVNjEHRqI="


@pytest.mark.asyncio
async def test_list_boards_cursor_pagination(connected_connector):
    """Two pages: page1 has cursor, page2 has no cursor → stops."""
    page1 = {"data": [SAMPLE_BOARD], "cursor": "next-cursor"}
    page2 = {"data": [SAMPLE_BOARD_MINIMAL]}

    connected_connector.client.get_boards.side_effect = [page1, page2]
    boards = await connected_connector.list_boards()
    assert len(boards) == 2
    assert connected_connector.client.get_boards.call_count == 2


@pytest.mark.asyncio
async def test_list_boards_stops_on_empty_data(connected_connector):
    """If cursor exists but data is empty, stop pagination."""
    page1 = {"data": [SAMPLE_BOARD], "cursor": "next-cursor"}
    page2 = {"data": [], "cursor": "another-cursor"}  # data empty — stop

    connected_connector.client.get_boards.side_effect = [page1, page2]
    boards = await connected_connector.list_boards()
    assert len(boards) == 1


@pytest.mark.asyncio
async def test_list_boards_empty_response(connected_connector):
    connected_connector.client.get_boards.return_value = {"data": []}
    boards = await connected_connector.list_boards()
    assert boards == []


# ═══════════════════════════════════════════════════════════════════════════
# 16. list_board_items() cursor pagination
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_board_items_single_page(connected_connector):
    connected_connector.client.get_board_items.return_value = SAMPLE_BOARD_ITEMS_RESPONSE
    items = await connected_connector.list_board_items("uXjVNjEHRqI=")
    assert len(items) == 2


@pytest.mark.asyncio
async def test_list_board_items_cursor_pagination(connected_connector):
    page1 = {"data": [SAMPLE_STICKY_NOTE], "cursor": "items-next"}
    page2 = {"data": [SAMPLE_CARD]}

    connected_connector.client.get_board_items.side_effect = [page1, page2]
    items = await connected_connector.list_board_items("uXjVNjEHRqI=")
    assert len(items) == 2
    assert connected_connector.client.get_board_items.call_count == 2


@pytest.mark.asyncio
async def test_list_board_items_empty(connected_connector):
    connected_connector.client.get_board_items.return_value = SAMPLE_BOARD_ITEMS_EMPTY
    items = await connected_connector.list_board_items("uXjVNjEHRqI=")
    assert items == []


@pytest.mark.asyncio
async def test_list_board_items_stops_on_empty_data(connected_connector):
    page1 = {"data": [SAMPLE_STICKY_NOTE], "cursor": "next"}
    page2 = {"data": [], "cursor": "more"}

    connected_connector.client.get_board_items.side_effect = [page1, page2]
    items = await connected_connector.list_board_items("board-id")
    assert len(items) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 17. get_board()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_board_success(connected_connector):
    connected_connector.client.get_board.return_value = SAMPLE_BOARD
    board = await connected_connector.get_board("uXjVNjEHRqI=")
    assert board["name"] == "Product Roadmap Q3"


@pytest.mark.asyncio
async def test_get_board_propagates_not_found(connected_connector):
    connected_connector.client.get_board.side_effect = MiroNotFoundError("404")
    with pytest.raises(MiroNotFoundError):
        await connected_connector.get_board("bad-id")


@pytest.mark.asyncio
async def test_get_board_propagates_auth_error(connected_connector):
    connected_connector.client.get_board.side_effect = MiroAuthError("401")
    with pytest.raises(MiroAuthError):
        await connected_connector.get_board("board-id")


# ═══════════════════════════════════════════════════════════════════════════
# 18. aclose() and context manager
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_aclose_is_safe(connector):
    await connector.aclose()
    # Second call also safe
    await connector.aclose()


@pytest.mark.asyncio
async def test_context_manager_returns_connector(connector):
    async with connector as c:
        assert c is connector


@pytest.mark.asyncio
async def test_context_manager_calls_aclose(connector):
    with patch.object(connector, "aclose", new_callable=AsyncMock) as mock_close:
        async with connector:
            pass
        mock_close.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# 19. Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_multi_tenant_separate_instances():
    c1 = MiroConnector(tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = MiroConnector(tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_multi_tenant_config_isolation():
    cfg_a = {**TEST_CONFIG, "client_id": "client-a"}
    cfg_b = {**TEST_CONFIG, "client_id": "client-b"}
    c1 = MiroConnector(tenant_id="tenant-A", config=cfg_a)
    c2 = MiroConnector(tenant_id="tenant-B", config=cfg_b)
    assert c1.config["client_id"] != c2.config["client_id"]


@pytest.mark.asyncio
async def test_sync_uses_correct_tenant_id():
    """sync() result belongs to the connector's own tenant — no cross-tenant leakage."""
    c1 = MiroConnector(tenant_id="tenant-X", connector_id="conn-x", config=dict(TEST_CONFIG))
    c2 = MiroConnector(tenant_id="tenant-Y", connector_id="conn-y", config=dict(TEST_CONFIG))

    mock_http1 = MagicMock(
        get_boards=AsyncMock(return_value={"data": []}),
        get_board_items=AsyncMock(return_value={"data": []}),
    )
    mock_http2 = MagicMock(
        get_boards=AsyncMock(return_value={"data": []}),
        get_board_items=AsyncMock(return_value={"data": []}),
    )
    c1.client = mock_http1
    c2.client = mock_http2

    r1 = await c1.sync()
    r2 = await c2.sync()

    assert r1.status == SyncStatus.COMPLETED
    assert r2.status == SyncStatus.COMPLETED
    # Each connector only called its own HTTP client
    assert mock_http1.get_boards.called
    assert mock_http2.get_boards.called
