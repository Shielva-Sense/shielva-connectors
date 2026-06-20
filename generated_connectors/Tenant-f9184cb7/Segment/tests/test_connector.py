"""Unit tests for SegmentConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import SegmentConnector, _normalize_function, _normalize_space
from exceptions import (
    SegmentAuthError,
    SegmentError,
    SegmentNetworkError,
    SegmentNotFoundError,
    SegmentRateLimitError,
    SegmentServerError,
)
from helpers.utils import CircuitBreaker, _stable_id, normalize_source, with_retry
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_segment_test_001"
VALID_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.segment_test_token"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_WORKSPACE_RESPONSE: dict = {
    "data": {
        "workspace": {
            "id": "ws_abc123",
            "name": "Acme Analytics",
            "slug": "acme-analytics",
        }
    }
}

SAMPLE_SOURCE: dict = {
    "id": "src_xyz789",
    "slug": "js-website",
    "name": "JS Website",
    "enabled": True,
    "workspaceId": "ws_abc123",
    "writeKey": "wk_secret123",
    "metadata": {
        "name": "JavaScript",
        "slug": "javascript",
        "description": "A JavaScript source for web tracking",
        "categories": ["website"],
    },
    "settings": {},
}

SAMPLE_SOURCES_RESPONSE: dict = {
    "data": {
        "sources": [SAMPLE_SOURCE],
        "pagination": {
            "current": "MA==",
            "next": None,
        },
    }
}

SAMPLE_SOURCES_PAGE_1: dict = {
    "data": {
        "sources": [SAMPLE_SOURCE],
        "pagination": {
            "current": "MA==",
            "next": "MjAw",
        },
    }
}

SAMPLE_SOURCES_PAGE_2: dict = {
    "data": {
        "sources": [
            {
                "id": "src_page2",
                "slug": "ios-app",
                "name": "iOS App",
                "enabled": False,
                "workspaceId": "ws_abc123",
                "writeKey": "wk_ios123",
                "metadata": {
                    "name": "iOS",
                    "slug": "ios",
                    "description": "iOS source",
                    "categories": ["mobile"],
                },
                "settings": {},
            }
        ],
        "pagination": {
            "current": "MjAw",
            "next": None,
        },
    }
}

SAMPLE_GET_SOURCE_RESPONSE: dict = {
    "data": {
        "source": SAMPLE_SOURCE,
    }
}

SAMPLE_DESTINATIONS_RESPONSE: dict = {
    "data": {
        "destinations": [
            {
                "id": "dest_abc",
                "name": "Mixpanel",
                "enabled": True,
                "sourceId": "src_xyz789",
            }
        ]
    }
}

SAMPLE_SPACES_RESPONSE: dict = {
    "data": {
        "spaces": [
            {
                "id": "space_001",
                "name": "Production Space",
                "slug": "production",
            }
        ]
    }
}

SAMPLE_FUNCTIONS_RESPONSE: dict = {
    "data": {
        "functions": [
            {
                "id": "fn_001",
                "displayName": "My Source Function",
                "slug": "my-source-function",
                "resourceType": "SOURCE",
                "createdAt": "2024-01-15T10:00:00Z",
                "updatedAt": "2024-06-01T09:00:00Z",
            }
        ],
        "pagination": {
            "current": "MA==",
            "next": None,
        },
    }
}


# ── Helper ────────────────────────────────────────────────────────────────────

def _make_connector(token: str = VALID_TOKEN) -> SegmentConnector:
    return SegmentConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"access_token": token},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# install() — 6 tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_missing_token() -> None:
    """install() without a token returns MISSING_CREDENTIALS."""
    conn = _make_connector(token="")
    result = await conn.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "required" in result.message


@pytest.mark.asyncio
async def test_install_success_with_workspace_name() -> None:
    """install() with a valid token returns HEALTHY + workspace name."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_workspace = AsyncMock(return_value=SAMPLE_WORKSPACE_RESPONSE)
    mock_client.aclose = AsyncMock()
    with patch.object(conn, "_make_client", return_value=mock_client):
        result = await conn.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Acme Analytics" in result.message


@pytest.mark.asyncio
async def test_install_success_without_workspace_name() -> None:
    """install() returns HEALTHY even when workspace name is empty."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_workspace = AsyncMock(return_value={"data": {"workspace": {}}})
    mock_client.aclose = AsyncMock()
    with patch.object(conn, "_make_client", return_value=mock_client):
        result = await conn.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_invalid_token() -> None:
    """install() with a rejected token returns INVALID_CREDENTIALS."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_workspace = AsyncMock(side_effect=SegmentAuthError("Unauthorized", 401))
    mock_client.aclose = AsyncMock()
    with patch.object(conn, "_make_client", return_value=mock_client):
        result = await conn.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid" in result.message


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    """install() on network failure returns OFFLINE + FAILED."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_workspace = AsyncMock(side_effect=SegmentNetworkError("connection refused"))
    mock_client.aclose = AsyncMock()
    with patch.object(conn, "_make_client", return_value=mock_client):
        result = await conn.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_connector_id() -> None:
    """install() populates connector_id in the result on success."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_workspace = AsyncMock(return_value=SAMPLE_WORKSPACE_RESPONSE)
    mock_client.aclose = AsyncMock()
    with patch.object(conn, "_make_client", return_value=mock_client):
        result = await conn.install()
    assert result.connector_id == CONNECTOR_ID


# ═══════════════════════════════════════════════════════════════════════════════
# health_check() — 5 tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_missing_token() -> None:
    """health_check() without a token returns MISSING_CREDENTIALS."""
    conn = _make_connector(token="")
    result = await conn.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_healthy() -> None:
    """health_check() returns HEALTHY with workspace name in message."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_workspace = AsyncMock(return_value=SAMPLE_WORKSPACE_RESPONSE)
    mock_client.aclose = AsyncMock()
    with patch.object(conn, "_make_client", return_value=mock_client):
        result = await conn.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Acme Analytics" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error() -> None:
    """health_check() with 401 returns OFFLINE + INVALID_CREDENTIALS."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_workspace = AsyncMock(side_effect=SegmentAuthError("Unauthorized", 401))
    mock_client.aclose = AsyncMock()
    with patch.object(conn, "_make_client", return_value=mock_client):
        result = await conn.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_degraded() -> None:
    """health_check() on network error returns DEGRADED before circuit opens."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_workspace = AsyncMock(side_effect=SegmentNetworkError("timeout"))
    mock_client.aclose = AsyncMock()
    with patch.object(conn, "_make_client", return_value=mock_client):
        result = await conn.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error_degraded() -> None:
    """health_check() on generic error returns DEGRADED."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_workspace = AsyncMock(side_effect=SegmentServerError("Server exploded", 500))
    mock_client.aclose = AsyncMock()
    with patch.object(conn, "_make_client", return_value=mock_client):
        result = await conn.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# sync() — 8 tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_completed_all_synced() -> None:
    """sync() completes when all sources, spaces, and functions succeed."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_sources = AsyncMock(return_value=SAMPLE_SOURCES_RESPONSE)
    mock_client.list_spaces = AsyncMock(return_value=SAMPLE_SPACES_RESPONSE)
    mock_client.list_functions = AsyncMock(return_value=SAMPLE_FUNCTIONS_RESPONSE)
    conn.http_client = mock_client
    result = await conn.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3  # 1 source + 1 space + 1 function
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_cursor_pagination() -> None:
    """sync() follows cursor pagination across multiple source pages."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_sources = AsyncMock(
        side_effect=[SAMPLE_SOURCES_PAGE_1, SAMPLE_SOURCES_PAGE_2]
    )
    mock_client.list_spaces = AsyncMock(return_value={"data": {"spaces": []}})
    mock_client.list_functions = AsyncMock(
        return_value={"data": {"functions": [], "pagination": {"current": "MA==", "next": None}}}
    )
    conn.http_client = mock_client
    result = await conn.sync()
    assert result.documents_found >= 2
    assert mock_client.list_sources.call_count == 2


@pytest.mark.asyncio
async def test_sync_fatal_error_returns_failed() -> None:
    """sync() returns FAILED when sources API errors immediately."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_sources = AsyncMock(side_effect=SegmentAuthError("Unauthorized", 401))
    conn.http_client = mock_client
    result = await conn.sync()
    assert result.status == SyncStatus.FAILED
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_spaces_error_nonfatal() -> None:
    """sync() continues even when spaces API fails (non-fatal)."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_sources = AsyncMock(return_value=SAMPLE_SOURCES_RESPONSE)
    mock_client.list_spaces = AsyncMock(side_effect=SegmentError("spaces unavailable"))
    mock_client.list_functions = AsyncMock(return_value=SAMPLE_FUNCTIONS_RESPONSE)
    conn.http_client = mock_client
    result = await conn.sync()
    # Should still have sources + functions synced
    assert result.documents_synced >= 1
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


@pytest.mark.asyncio
async def test_sync_functions_error_nonfatal() -> None:
    """sync() continues even when functions API fails (non-fatal)."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_sources = AsyncMock(return_value=SAMPLE_SOURCES_RESPONSE)
    mock_client.list_spaces = AsyncMock(return_value=SAMPLE_SPACES_RESPONSE)
    mock_client.list_functions = AsyncMock(side_effect=SegmentError("functions unavailable"))
    conn.http_client = mock_client
    result = await conn.sync()
    assert result.documents_synced >= 1


@pytest.mark.asyncio
async def test_sync_partial_when_normalize_fails() -> None:
    """sync() returns PARTIAL when some documents fail to normalize."""
    conn = _make_connector()
    mock_client = AsyncMock()
    # Corrupt source — missing all fields
    mock_client.list_sources = AsyncMock(
        return_value={
            "data": {
                "sources": [None, SAMPLE_SOURCE],  # None will fail normalize_source
                "pagination": {"current": "MA==", "next": None},
            }
        }
    )
    mock_client.list_spaces = AsyncMock(return_value={"data": {"spaces": []}})
    mock_client.list_functions = AsyncMock(
        return_value={"data": {"functions": [], "pagination": {"current": "MA==", "next": None}}}
    )
    conn.http_client = mock_client
    result = await conn.sync()
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_ingest_called_with_kb_id() -> None:
    """sync() calls _ingest_document when kb_id is provided."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_sources = AsyncMock(return_value=SAMPLE_SOURCES_RESPONSE)
    mock_client.list_spaces = AsyncMock(return_value={"data": {"spaces": []}})
    mock_client.list_functions = AsyncMock(
        return_value={"data": {"functions": [], "pagination": {"current": "MA==", "next": None}}}
    )
    conn.http_client = mock_client
    conn._ingest_document = AsyncMock()
    await conn.sync(kb_id="kb_test_001")
    assert conn._ingest_document.await_count >= 1


@pytest.mark.asyncio
async def test_sync_empty_workspace_completes() -> None:
    """sync() with no sources/spaces/functions returns COMPLETED with zero docs."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_sources = AsyncMock(
        return_value={"data": {"sources": [], "pagination": {"current": "MA==", "next": None}}}
    )
    mock_client.list_spaces = AsyncMock(return_value={"data": {"spaces": []}})
    mock_client.list_functions = AsyncMock(
        return_value={"data": {"functions": [], "pagination": {"current": "MA==", "next": None}}}
    )
    conn.http_client = mock_client
    result = await conn.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Public API methods — 9 tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_workspaces_delegates_to_client() -> None:
    """list_workspaces() proxies to client.get_workspace."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_workspace = AsyncMock(return_value=SAMPLE_WORKSPACE_RESPONSE)
    conn.http_client = mock_client
    result = await conn.list_workspaces()
    assert result == SAMPLE_WORKSPACE_RESPONSE
    mock_client.get_workspace.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_sources_no_cursor() -> None:
    """list_sources() with no cursor calls client correctly."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_sources = AsyncMock(return_value=SAMPLE_SOURCES_RESPONSE)
    conn.http_client = mock_client
    result = await conn.list_sources()
    assert result == SAMPLE_SOURCES_RESPONSE
    mock_client.list_sources.assert_awaited_once_with(
        pagination_cursor=None, count=200
    )


@pytest.mark.asyncio
async def test_list_sources_with_cursor() -> None:
    """list_sources() passes pagination cursor to client."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_sources = AsyncMock(return_value=SAMPLE_SOURCES_PAGE_2)
    conn.http_client = mock_client
    result = await conn.list_sources(pagination_cursor="MjAw")
    mock_client.list_sources.assert_awaited_once_with(
        pagination_cursor="MjAw", count=200
    )
    assert result == SAMPLE_SOURCES_PAGE_2


@pytest.mark.asyncio
async def test_get_source() -> None:
    """get_source() calls client.get_source with correct ID."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.get_source = AsyncMock(return_value=SAMPLE_GET_SOURCE_RESPONSE)
    conn.http_client = mock_client
    result = await conn.get_source("src_xyz789")
    assert result == SAMPLE_GET_SOURCE_RESPONSE
    mock_client.get_source.assert_awaited_once_with("src_xyz789")


@pytest.mark.asyncio
async def test_list_destinations() -> None:
    """list_destinations() calls client.list_destinations with source_id."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_destinations = AsyncMock(return_value=SAMPLE_DESTINATIONS_RESPONSE)
    conn.http_client = mock_client
    result = await conn.list_destinations("src_xyz789")
    assert result == SAMPLE_DESTINATIONS_RESPONSE
    mock_client.list_destinations.assert_awaited_once_with("src_xyz789")


@pytest.mark.asyncio
async def test_list_spaces() -> None:
    """list_spaces() proxies to client.list_spaces."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_spaces = AsyncMock(return_value=SAMPLE_SPACES_RESPONSE)
    conn.http_client = mock_client
    result = await conn.list_spaces()
    assert result == SAMPLE_SPACES_RESPONSE
    mock_client.list_spaces.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_functions_no_cursor() -> None:
    """list_functions() with no cursor calls client correctly."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_functions = AsyncMock(return_value=SAMPLE_FUNCTIONS_RESPONSE)
    conn.http_client = mock_client
    result = await conn.list_functions()
    assert result == SAMPLE_FUNCTIONS_RESPONSE
    mock_client.list_functions.assert_awaited_once_with(
        pagination_cursor=None, count=200
    )


@pytest.mark.asyncio
async def test_list_functions_with_cursor() -> None:
    """list_functions() passes pagination cursor."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.list_functions = AsyncMock(return_value=SAMPLE_FUNCTIONS_RESPONSE)
    conn.http_client = mock_client
    await conn.list_functions(pagination_cursor="MjAw")
    mock_client.list_functions.assert_awaited_once_with(
        pagination_cursor="MjAw", count=200
    )


@pytest.mark.asyncio
async def test_ensure_client_creates_on_first_call() -> None:
    """_ensure_client() initializes http_client if not already set."""
    conn = _make_connector()
    assert conn.http_client is None
    client = conn._ensure_client()
    assert conn.http_client is not None
    assert client is conn.http_client


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_source() — 8 tests
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_source_full_fields() -> None:
    """normalize_source() maps all fields correctly."""
    doc = normalize_source(SAMPLE_SOURCE, CONNECTOR_ID, TENANT_ID)
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "JS Website" in doc.title
    assert "src_xyz789" in doc.content
    assert "js-website" in doc.content
    assert "website" in doc.content  # category
    assert doc.metadata["source_id"] == "src_xyz789"
    assert doc.metadata["enabled"] is True


def test_normalize_source_stable_id() -> None:
    """normalize_source() produces a stable 16-char hex source_id."""
    doc = normalize_source(SAMPLE_SOURCE, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16
    assert all(c in "0123456789abcdef" for c in doc.source_id)


def test_normalize_source_stable_id_deterministic() -> None:
    """normalize_source() produces the same ID for the same input."""
    doc1 = normalize_source(SAMPLE_SOURCE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_source(SAMPLE_SOURCE, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_source_uses_slug_as_fallback_name() -> None:
    """normalize_source() falls back to slug when name is empty."""
    source = dict(SAMPLE_SOURCE, name="")
    doc = normalize_source(source, CONNECTOR_ID, TENANT_ID)
    assert "js-website" in doc.title


def test_normalize_source_disabled_source() -> None:
    """normalize_source() reflects enabled=False in content."""
    source = dict(SAMPLE_SOURCE, enabled=False)
    doc = normalize_source(source, CONNECTOR_ID, TENANT_ID)
    assert "Enabled: False" in doc.content


def test_normalize_source_source_url_contains_slug() -> None:
    """normalize_source() builds source_url with slug."""
    doc = normalize_source(SAMPLE_SOURCE, CONNECTOR_ID, TENANT_ID)
    assert "js-website" in doc.source_url


def test_normalize_source_empty_metadata() -> None:
    """normalize_source() handles missing metadata gracefully."""
    source = {k: v for k, v in SAMPLE_SOURCE.items() if k != "metadata"}
    source["metadata"] = {}
    doc = normalize_source(source, CONNECTOR_ID, TENANT_ID)
    assert doc.title.startswith("Segment source:")


def test_normalize_source_empty_categories() -> None:
    """normalize_source() handles empty categories without error."""
    source = dict(SAMPLE_SOURCE)
    source["metadata"] = dict(SAMPLE_SOURCE["metadata"], categories=[])
    doc = normalize_source(source, CONNECTOR_ID, TENANT_ID)
    assert "Categories:" not in doc.content


# ═══════════════════════════════════════════════════════════════════════════════
# _normalize_space() and _normalize_function() — 6 tests
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_space_basic() -> None:
    """_normalize_space() returns correct title and content."""
    space = {"id": "space_001", "name": "Production Space", "slug": "production"}
    doc = _normalize_space(space, CONNECTOR_ID, TENANT_ID)
    assert "Production Space" in doc.title
    assert "space_001" in doc.content
    assert doc.metadata["space_id"] == "space_001"


def test_normalize_space_stable_id() -> None:
    """_normalize_space() produces 16-char stable ID."""
    space = {"id": "space_001", "name": "Production Space", "slug": "production"}
    doc = _normalize_space(space, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_function_basic() -> None:
    """_normalize_function() returns correct title and content."""
    fn = {
        "id": "fn_001",
        "displayName": "My Source Function",
        "slug": "my-source-function",
        "resourceType": "SOURCE",
        "createdAt": "2024-01-15T10:00:00Z",
        "updatedAt": "2024-06-01T09:00:00Z",
    }
    doc = _normalize_function(fn, CONNECTOR_ID, TENANT_ID)
    assert "My Source Function" in doc.title
    assert "fn_001" in doc.content
    assert "SOURCE" in doc.content
    assert doc.metadata["resource_type"] == "SOURCE"


def test_normalize_function_stable_id() -> None:
    """_normalize_function() produces 16-char stable ID."""
    fn = {"id": "fn_001", "displayName": "My Fn", "resourceType": "SOURCE"}
    doc = _normalize_function(fn, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_function_missing_display_name() -> None:
    """_normalize_function() falls back to slug when displayName is absent."""
    fn = {"id": "fn_002", "slug": "backup-fn", "resourceType": "DESTINATION"}
    doc = _normalize_function(fn, CONNECTOR_ID, TENANT_ID)
    assert "backup-fn" in doc.title


def test_normalize_function_timestamps_in_content() -> None:
    """_normalize_function() includes created/updated timestamps in content."""
    fn = {
        "id": "fn_003",
        "displayName": "Time Fn",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-06-01T00:00:00Z",
    }
    doc = _normalize_function(fn, CONNECTOR_ID, TENANT_ID)
    assert "Created" in doc.content
    assert "Updated" in doc.content


# ═══════════════════════════════════════════════════════════════════════════════
# _stable_id utility — 4 tests
# ═══════════════════════════════════════════════════════════════════════════════


def test_stable_id_length() -> None:
    """_stable_id() returns 16 hex chars."""
    sid = _stable_id("src_xyz789")
    assert len(sid) == 16


def test_stable_id_hex_only() -> None:
    """_stable_id() output is hexadecimal only."""
    sid = _stable_id("src_xyz789")
    assert all(c in "0123456789abcdef" for c in sid)


def test_stable_id_deterministic() -> None:
    """_stable_id() is deterministic for the same input."""
    assert _stable_id("src_xyz789") == _stable_id("src_xyz789")


def test_stable_id_different_for_different_inputs() -> None:
    """_stable_id() produces different values for different inputs."""
    assert _stable_id("src_aaa") != _stable_id("src_bbb")


# ═══════════════════════════════════════════════════════════════════════════════
# with_retry() — 6 tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_succeeds_first_attempt() -> None:
    """with_retry() returns result when fn succeeds on first attempt."""
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == {"ok": True}
    assert fn.await_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_segment_error() -> None:
    """with_retry() retries transient SegmentError and succeeds."""
    fn = AsyncMock(
        side_effect=[SegmentError("transient"), SegmentError("transient"), {"ok": True}]
    )
    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == {"ok": True}
    assert fn.await_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    """with_retry() raises SegmentAuthError immediately without retrying."""
    fn = AsyncMock(side_effect=SegmentAuthError("bad token"))
    with pytest.raises(SegmentAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert fn.await_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts_and_raises() -> None:
    """with_retry() raises after exhausting all attempts."""
    fn = AsyncMock(side_effect=SegmentError("always fails"))
    with pytest.raises(SegmentError):
        await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert fn.await_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    """with_retry() uses retry_after from SegmentRateLimitError."""
    import asyncio as _asyncio
    calls: list[float] = []

    async def _fn() -> dict:
        raise SegmentRateLimitError("rate limited", retry_after=0.0)

    fn = AsyncMock(side_effect=_fn)
    with pytest.raises(SegmentRateLimitError):
        await with_retry(fn, max_attempts=2, base_delay=0.0)
    assert fn.await_count == 2


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    """with_retry() forwards positional and keyword arguments to fn."""
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", key="val", max_attempts=1, base_delay=0.0)
    fn.assert_awaited_once_with("arg1", key="val")
    assert result == "result"


# ═══════════════════════════════════════════════════════════════════════════════
# CircuitBreaker — 5 tests
# ═══════════════════════════════════════════════════════════════════════════════


def test_circuit_breaker_starts_closed() -> None:
    """CircuitBreaker initializes in closed state."""
    cb = CircuitBreaker(failure_threshold=3)
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_opens_after_threshold() -> None:
    """CircuitBreaker transitions to open after failure_threshold failures."""
    cb = CircuitBreaker(failure_threshold=3)
    cb.on_failure()
    cb.on_failure()
    assert cb.state == "closed"
    cb.on_failure()
    assert cb.is_open


def test_circuit_breaker_resets_on_success() -> None:
    """CircuitBreaker resets to closed on success."""
    cb = CircuitBreaker(failure_threshold=2)
    cb.on_failure()
    cb.on_failure()
    assert cb.is_open
    cb.on_success()
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_half_open_after_timeout() -> None:
    """CircuitBreaker transitions to half-open after recovery_timeout_s elapses."""
    import time
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=60.0)
    cb.on_failure()
    # Confirm open while timeout has not elapsed
    assert cb.is_open
    # Simulate that the recovery timeout has elapsed
    cb._opened_at = time.monotonic() - 61.0
    assert cb.state == "half-open"


def test_circuit_breaker_single_failure_not_open() -> None:
    """CircuitBreaker with threshold 5 stays closed on one failure."""
    cb = CircuitBreaker(failure_threshold=5)
    cb.on_failure()
    assert not cb.is_open
    assert cb.state == "closed"


# ═══════════════════════════════════════════════════════════════════════════════
# Exception hierarchy — 5 tests
# ═══════════════════════════════════════════════════════════════════════════════


def test_segment_error_base() -> None:
    """SegmentError stores message, status_code, and code."""
    exc = SegmentError("something failed", status_code=500, code="internal")
    assert str(exc) == "something failed"
    assert exc.status_code == 500
    assert exc.code == "internal"


def test_segment_auth_error_is_segment_error() -> None:
    """SegmentAuthError is a subclass of SegmentError."""
    exc = SegmentAuthError("unauthorized", 401)
    assert isinstance(exc, SegmentError)
    assert exc.status_code == 401


def test_segment_rate_limit_has_retry_after() -> None:
    """SegmentRateLimitError stores retry_after."""
    exc = SegmentRateLimitError("rate limited", retry_after=30.5)
    assert exc.retry_after == 30.5
    assert exc.status_code == 429


def test_segment_not_found_error_message() -> None:
    """SegmentNotFoundError includes resource type and ID in message."""
    exc = SegmentNotFoundError("source", "src_xyz789")
    assert "src_xyz789" in str(exc)
    assert exc.status_code == 404


def test_segment_server_error_is_segment_error() -> None:
    """SegmentServerError is a subclass of SegmentError."""
    exc = SegmentServerError("server crashed", 503)
    assert isinstance(exc, SegmentError)
    assert exc.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════════
# Lifecycle — 4 tests
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_releases_client() -> None:
    """aclose() closes the underlying http_client and sets it to None."""
    conn = _make_connector()
    mock_client = AsyncMock()
    mock_client.aclose = AsyncMock()
    conn.http_client = mock_client
    await conn.aclose()
    mock_client.aclose.assert_awaited_once()
    assert conn.http_client is None


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    """aclose() on an already-closed connector does not raise."""
    conn = _make_connector()
    conn.http_client = None
    await conn.aclose()  # should not raise


@pytest.mark.asyncio
async def test_async_context_manager() -> None:
    """SegmentConnector works as an async context manager."""
    async with SegmentConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"access_token": VALID_TOKEN},
    ) as conn:
        assert isinstance(conn, SegmentConnector)


def test_connector_type_and_auth_type() -> None:
    """CONNECTOR_TYPE and AUTH_TYPE class constants are correct."""
    assert SegmentConnector.CONNECTOR_TYPE == "segment"
    assert SegmentConnector.AUTH_TYPE == "api_key"
