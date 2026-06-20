"""Unit tests for GoogleSheetsConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import GoogleSheetsConnector
from exceptions import (
    GoogleSheetsAuthError,
    GoogleSheetsNetworkError,
    GoogleSheetsNotFoundError,
    GoogleSheetsRateLimitError,
)
from helpers.utils import normalize_sheet_rows, normalize_spreadsheet, with_retry
from models import AuthStatus, ConnectorHealth, ConnectorDocument, SyncStatus

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_gsheets_test_001"
VALID_TOKEN = "ya29.test_access_token"
CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"

SAMPLE_SPREADSHEET: dict = {
    "spreadsheetId": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "properties": {"title": "Employee Data"},
    "sheets": [
        {"properties": {"title": "Sheet1", "sheetId": 0}},
        {"properties": {"title": "Contacts", "sheetId": 1}},
    ],
}

SAMPLE_VALUES: dict = {
    "range": "Sheet1!A1:Z1000",
    "majorDimension": "ROWS",
    "values": [
        ["Name", "Email", "Department"],
        ["Alice Smith", "alice@example.com", "Engineering"],
        ["Bob Jones", "bob@example.com", "Sales"],
        ["Carol Chen", "carol@example.com", "Design"],
    ],
}

SAMPLE_USERINFO: dict = {
    "id": "123456789",
    "email": "user@example.com",
    "verified_email": True,
    "name": "Test User",
}

SAMPLE_DRIVE_LIST: dict = {
    "files": [
        {
            "id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
            "name": "Employee Data",
            "modifiedTime": "2026-06-01T12:00:00.000Z",
        },
        {
            "id": "2CyiNWt1YSB6oGNLwCeCajhhnVVrqumsct85PhWF3vnt",
            "name": "Sales Pipeline",
            "modifiedTime": "2026-06-10T08:00:00.000Z",
        },
    ],
    "nextPageToken": None,
}

EMPTY_VALUES: dict = {"range": "Sheet1!A1:Z1000", "majorDimension": "ROWS", "values": []}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> GoogleSheetsConnector:
    c = GoogleSheetsConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": VALID_TOKEN,
        },
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    return c


@pytest.fixture()
def no_token() -> GoogleSheetsConnector:
    return GoogleSheetsConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )


@pytest.fixture()
def no_creds() -> GoogleSheetsConnector:
    return GoogleSheetsConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_missing_both_creds(no_creds: GoogleSheetsConnector) -> None:
    result = await no_creds.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id and client_secret are required" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    c = GoogleSheetsConnector(
        config={"client_secret": CLIENT_SECRET, "access_token": VALID_TOKEN}
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    c = GoogleSheetsConnector(
        config={"client_id": CLIENT_ID, "access_token": VALID_TOKEN}
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_pending_no_access_token(no_token: GoogleSheetsConnector) -> None:
    """Credentials present but OAuth flow not yet completed → PENDING."""
    result = await no_token.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert "OAuth" in result.message


@pytest.mark.asyncio
async def test_install_success_with_token() -> None:
    c = GoogleSheetsConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": VALID_TOKEN,
        },
    )
    with patch("connector.GoogleSheetsHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_userinfo = AsyncMock(return_value=SAMPLE_USERINFO)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "user@example.com" in result.message


@pytest.mark.asyncio
async def test_install_invalid_token() -> None:
    c = GoogleSheetsConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": "expired_token",
        }
    )
    with patch("connector.GoogleSheetsHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_userinfo = AsyncMock(
            side_effect=GoogleSheetsAuthError("Token expired", 401)
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Token expired" in result.message


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = GoogleSheetsConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": VALID_TOKEN,
        }
    )
    with patch("connector.GoogleSheetsHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_userinfo = AsyncMock(
            side_effect=GoogleSheetsNetworkError("Connection refused")
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ── health_check() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_no_token(no_token: GoogleSheetsConnector) -> None:
    result = await no_token.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token is required" in result.message


@pytest.mark.asyncio
async def test_health_check_healthy(authed: GoogleSheetsConnector) -> None:
    with patch("connector.GoogleSheetsHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_userinfo = AsyncMock(return_value=SAMPLE_USERINFO)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "user@example.com" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: GoogleSheetsConnector) -> None:
    with patch("connector.GoogleSheetsHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_userinfo = AsyncMock(
            side_effect=GoogleSheetsAuthError("Unauthorized", 401)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: GoogleSheetsConnector) -> None:
    with patch("connector.GoogleSheetsHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_userinfo = AsyncMock(
            side_effect=GoogleSheetsNetworkError("Timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(authed: GoogleSheetsConnector) -> None:
    with patch("connector.GoogleSheetsHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_userinfo = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync() ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_no_token() -> None:
    c = GoogleSheetsConnector(
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    )
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_sync_empty_drive(authed: GoogleSheetsConnector) -> None:
    authed.http_client.list_spreadsheets = AsyncMock(
        return_value={"files": [], "nextPageToken": None}
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_one_spreadsheet(authed: GoogleSheetsConnector) -> None:
    authed.http_client.list_spreadsheets = AsyncMock(
        return_value={
            "files": [{"id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms", "name": "Employee Data"}],
            "nextPageToken": None,
        }
    )
    authed.http_client.get_spreadsheet = AsyncMock(return_value=SAMPLE_SPREADSHEET)
    authed.http_client.get_values = AsyncMock(return_value=SAMPLE_VALUES)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    # 1 spreadsheet doc + 3 data rows per sheet (2 sheets) = 1 + 6 = 7
    assert result.documents_found == 7
    assert result.documents_synced == 7
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(authed: GoogleSheetsConnector) -> None:
    authed.http_client.list_spreadsheets = AsyncMock(
        return_value={
            "files": [{"id": "sp_id_1", "name": "Data"}],
            "nextPageToken": None,
        }
    )
    authed.http_client.get_spreadsheet = AsyncMock(
        return_value={
            "spreadsheetId": "sp_id_1",
            "properties": {"title": "Data"},
            "sheets": [{"properties": {"title": "Sheet1"}}],
        }
    )
    authed.http_client.get_values = AsyncMock(return_value=SAMPLE_VALUES)
    ingest_calls: list = []

    async def mock_ingest(doc: ConnectorDocument, kb_id: str) -> None:
        ingest_calls.append((doc.source_id, kb_id))

    authed._ingest_document = mock_ingest  # type: ignore[method-assign]
    result = await authed.sync(full=True, kb_id="kb_test_123")
    assert result.documents_synced > 0
    assert all(kb_id == "kb_test_123" for _, kb_id in ingest_calls)


@pytest.mark.asyncio
async def test_sync_drive_list_fails(authed: GoogleSheetsConnector) -> None:
    from exceptions import GoogleSheetsNetworkError
    authed.http_client.list_spreadsheets = AsyncMock(
        side_effect=GoogleSheetsNetworkError("Network failure")
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "Failed to list spreadsheets" in result.message


@pytest.mark.asyncio
async def test_sync_pagination(authed: GoogleSheetsConnector) -> None:
    page1 = {
        "files": [{"id": "sp_id_1", "name": "Sheet A"}],
        "nextPageToken": "token_abc",
    }
    page2 = {
        "files": [{"id": "sp_id_2", "name": "Sheet B"}],
        "nextPageToken": None,
    }
    authed.http_client.list_spreadsheets = AsyncMock(side_effect=[page1, page2])
    authed.http_client.get_spreadsheet = AsyncMock(
        return_value={
            "spreadsheetId": "sp_id_1",
            "properties": {"title": "Sheet A"},
            "sheets": [],
        }
    )
    result = await authed.sync(full=True)
    assert authed.http_client.list_spreadsheets.call_count == 2
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_empty_sheet_skipped(authed: GoogleSheetsConnector) -> None:
    authed.http_client.list_spreadsheets = AsyncMock(
        return_value={
            "files": [{"id": "sp_id_1", "name": "Empty Sheet"}],
            "nextPageToken": None,
        }
    )
    authed.http_client.get_spreadsheet = AsyncMock(
        return_value={
            "spreadsheetId": "sp_id_1",
            "properties": {"title": "Empty Sheet"},
            "sheets": [{"properties": {"title": "Sheet1"}}],
        }
    )
    authed.http_client.get_values = AsyncMock(return_value=EMPTY_VALUES)
    result = await authed.sync(full=True)
    # Only the spreadsheet-level document (no row docs because values is empty)
    assert result.documents_found == 1
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_headers_only_no_rows(authed: GoogleSheetsConnector) -> None:
    """Sheet with only a header row produces no row documents."""
    authed.http_client.list_spreadsheets = AsyncMock(
        return_value={
            "files": [{"id": "sp_id_1", "name": "Headers Only"}],
            "nextPageToken": None,
        }
    )
    authed.http_client.get_spreadsheet = AsyncMock(
        return_value={
            "spreadsheetId": "sp_id_1",
            "properties": {"title": "Headers Only"},
            "sheets": [{"properties": {"title": "Sheet1"}}],
        }
    )
    authed.http_client.get_values = AsyncMock(
        return_value={"values": [["Name", "Email"]]}
    )
    result = await authed.sync(full=True)
    # 1 spreadsheet doc + 0 row docs
    assert result.documents_found == 1


# ── list_spreadsheets() ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_spreadsheets_single_page(authed: GoogleSheetsConnector) -> None:
    authed.http_client.list_spreadsheets = AsyncMock(return_value=SAMPLE_DRIVE_LIST)
    files = await authed.list_spreadsheets()
    assert len(files) == 2
    assert files[0]["name"] == "Employee Data"


@pytest.mark.asyncio
async def test_list_spreadsheets_multi_page(authed: GoogleSheetsConnector) -> None:
    page1 = {"files": [{"id": "a", "name": "A"}], "nextPageToken": "tok1"}
    page2 = {"files": [{"id": "b", "name": "B"}, {"id": "c", "name": "C"}], "nextPageToken": None}
    authed.http_client.list_spreadsheets = AsyncMock(side_effect=[page1, page2])
    files = await authed.list_spreadsheets()
    assert len(files) == 3
    assert authed.http_client.list_spreadsheets.call_count == 2


@pytest.mark.asyncio
async def test_list_spreadsheets_empty(authed: GoogleSheetsConnector) -> None:
    authed.http_client.list_spreadsheets = AsyncMock(
        return_value={"files": [], "nextPageToken": None}
    )
    files = await authed.list_spreadsheets()
    assert files == []


# ── get_spreadsheet() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_spreadsheet_success(authed: GoogleSheetsConnector) -> None:
    authed.http_client.get_spreadsheet = AsyncMock(return_value=SAMPLE_SPREADSHEET)
    result = await authed.get_spreadsheet("1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms")
    assert result["spreadsheetId"] == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
    assert result["properties"]["title"] == "Employee Data"
    assert len(result["sheets"]) == 2


@pytest.mark.asyncio
async def test_get_spreadsheet_not_found(authed: GoogleSheetsConnector) -> None:
    authed.http_client.get_spreadsheet = AsyncMock(
        side_effect=GoogleSheetsNotFoundError("spreadsheet", "nonexistent_id")
    )
    with pytest.raises(GoogleSheetsNotFoundError):
        await authed.get_spreadsheet("nonexistent_id")


@pytest.mark.asyncio
async def test_get_spreadsheet_auth_error(authed: GoogleSheetsConnector) -> None:
    authed.http_client.get_spreadsheet = AsyncMock(
        side_effect=GoogleSheetsAuthError("Forbidden", 403)
    )
    with pytest.raises(GoogleSheetsAuthError):
        await authed.get_spreadsheet("some_id")


# ── get_sheet_values() ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_sheet_values_success(authed: GoogleSheetsConnector) -> None:
    authed.http_client.get_values = AsyncMock(return_value=SAMPLE_VALUES)
    result = await authed.get_sheet_values("sp_id", "Sheet1")
    assert result["values"][0] == ["Name", "Email", "Department"]
    authed.http_client.get_values.assert_called_once()
    # Verify range format
    call_args = authed.http_client.get_values.call_args
    assert "'Sheet1'!A:Z" in str(call_args)


@pytest.mark.asyncio
async def test_get_sheet_values_empty(authed: GoogleSheetsConnector) -> None:
    authed.http_client.get_values = AsyncMock(return_value=EMPTY_VALUES)
    result = await authed.get_sheet_values("sp_id", "Sheet1")
    assert result.get("values", []) == []


@pytest.mark.asyncio
async def test_get_sheet_values_sheet_with_spaces(authed: GoogleSheetsConnector) -> None:
    """Sheet names with spaces must be quoted in the range."""
    authed.http_client.get_values = AsyncMock(return_value=SAMPLE_VALUES)
    await authed.get_sheet_values("sp_id", "My Data Sheet")
    call_args = authed.http_client.get_values.call_args
    assert "'My Data Sheet'!A:Z" in str(call_args)


# ── get_spreadsheet_data() ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_spreadsheet_data_full(authed: GoogleSheetsConnector) -> None:
    authed.http_client.get_spreadsheet = AsyncMock(return_value=SAMPLE_SPREADSHEET)
    authed.http_client.get_values = AsyncMock(return_value=SAMPLE_VALUES)
    data = await authed.get_spreadsheet_data("sp_id")
    assert "Sheet1" in data
    assert "Contacts" in data
    assert len(data["Sheet1"]) == 3
    assert data["Sheet1"][0]["Name"] == "Alice Smith"
    assert data["Sheet1"][0]["Email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_get_spreadsheet_data_empty_sheet(authed: GoogleSheetsConnector) -> None:
    spreadsheet = {
        "spreadsheetId": "sp_id",
        "properties": {"title": "Test"},
        "sheets": [{"properties": {"title": "Empty"}}],
    }
    authed.http_client.get_spreadsheet = AsyncMock(return_value=spreadsheet)
    authed.http_client.get_values = AsyncMock(return_value=EMPTY_VALUES)
    data = await authed.get_spreadsheet_data("sp_id")
    assert data["Empty"] == []


@pytest.mark.asyncio
async def test_get_spreadsheet_data_no_sheets(authed: GoogleSheetsConnector) -> None:
    spreadsheet = {
        "spreadsheetId": "sp_id",
        "properties": {"title": "No Sheets"},
        "sheets": [],
    }
    authed.http_client.get_spreadsheet = AsyncMock(return_value=spreadsheet)
    data = await authed.get_spreadsheet_data("sp_id")
    assert data == {}


# ── normalize_sheet_rows() ───────────────────────────────────────────────────


def test_normalize_sheet_rows_basic() -> None:
    headers = ["Name", "Email", "Department"]
    rows = [
        ["Alice Smith", "alice@example.com", "Engineering"],
        ["Bob Jones", "bob@example.com", "Sales"],
    ]
    docs = normalize_sheet_rows(
        "sp_id_001", "Sheet1", headers, rows, CONNECTOR_ID, TENANT_ID
    )
    assert len(docs) == 2
    assert docs[0].title == "Sheet1 — Row 2"
    assert docs[1].title == "Sheet1 — Row 3"
    assert docs[0].tenant_id == TENANT_ID
    assert docs[0].connector_id == CONNECTOR_ID
    assert "Alice Smith" in docs[0].content
    assert "Name: Alice Smith" in docs[0].content


def test_normalize_sheet_rows_stable_ids() -> None:
    headers = ["Col1"]
    rows = [["val1"]]
    docs1 = normalize_sheet_rows("sp_id", "Sheet1", headers, rows, CONNECTOR_ID, TENANT_ID)
    docs2 = normalize_sheet_rows("sp_id", "Sheet1", headers, rows, CONNECTOR_ID, TENANT_ID)
    assert docs1[0].source_id == docs2[0].source_id


def test_normalize_sheet_rows_id_is_16_chars() -> None:
    docs = normalize_sheet_rows(
        "sp_id", "Sheet1", ["A"], [["val"]], CONNECTOR_ID, TENANT_ID
    )
    assert len(docs[0].source_id) == 16


def test_normalize_sheet_rows_different_spreadsheets_different_ids() -> None:
    headers = ["Col1"]
    rows = [["val1"]]
    docs1 = normalize_sheet_rows("sp_id_A", "Sheet1", headers, rows, CONNECTOR_ID, TENANT_ID)
    docs2 = normalize_sheet_rows("sp_id_B", "Sheet1", headers, rows, CONNECTOR_ID, TENANT_ID)
    assert docs1[0].source_id != docs2[0].source_id


def test_normalize_sheet_rows_skips_empty_cells() -> None:
    headers = ["Name", "Email", "Notes"]
    rows = [["Alice", "alice@example.com", ""]]
    docs = normalize_sheet_rows("sp_id", "Sheet1", headers, rows, CONNECTOR_ID, TENANT_ID)
    assert "Notes" not in docs[0].content
    assert "alice@example.com" in docs[0].content


def test_normalize_sheet_rows_empty_row_content() -> None:
    headers = ["Name", "Email"]
    rows = [["", ""]]
    docs = normalize_sheet_rows("sp_id", "Sheet1", headers, rows, CONNECTOR_ID, TENANT_ID)
    assert docs[0].content == "(empty row)"


def test_normalize_sheet_rows_short_row() -> None:
    """Row with fewer cells than headers should not raise IndexError."""
    headers = ["Name", "Email", "Department"]
    rows = [["Alice"]]  # only one cell
    docs = normalize_sheet_rows("sp_id", "Sheet1", headers, rows, CONNECTOR_ID, TENANT_ID)
    assert len(docs) == 1
    assert "Alice" in docs[0].content


def test_normalize_sheet_rows_metadata_structure() -> None:
    headers = ["Name", "Email"]
    rows = [["Alice", "alice@example.com"]]
    docs = normalize_sheet_rows("sp_id", "Sheet1", headers, rows, CONNECTOR_ID, TENANT_ID)
    meta = docs[0].metadata
    assert meta["spreadsheet_id"] == "sp_id"
    assert meta["sheet_title"] == "Sheet1"
    assert meta["row_index"] == 0
    assert meta["row_number"] == 2
    assert meta["headers"] == ["Name", "Email"]
    assert meta["values_dict"]["Name"] == "Alice"


def test_normalize_sheet_rows_source_url_contains_id() -> None:
    docs = normalize_sheet_rows("sp_id_XYZ", "Sheet1", ["Col"], [["val"]], CONNECTOR_ID, TENANT_ID)
    assert "sp_id_XYZ" in docs[0].source_url


def test_normalize_sheet_rows_empty_input() -> None:
    docs = normalize_sheet_rows("sp_id", "Sheet1", [], [], CONNECTOR_ID, TENANT_ID)
    assert docs == []


# ── normalize_spreadsheet() ──────────────────────────────────────────────────


def test_normalize_spreadsheet_basic() -> None:
    doc = normalize_spreadsheet(SAMPLE_SPREADSHEET, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Employee Data"
    assert "Sheet1" in doc.content
    assert "Contacts" in doc.content
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_spreadsheet_stable_id() -> None:
    doc1 = normalize_spreadsheet(SAMPLE_SPREADSHEET, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_spreadsheet(SAMPLE_SPREADSHEET, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_spreadsheet_id_is_16_chars() -> None:
    doc = normalize_spreadsheet(SAMPLE_SPREADSHEET, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_spreadsheet_source_url_format() -> None:
    doc = normalize_spreadsheet(SAMPLE_SPREADSHEET, CONNECTOR_ID, TENANT_ID)
    assert "docs.google.com/spreadsheets/d/" in doc.source_url
    assert SAMPLE_SPREADSHEET["spreadsheetId"] in doc.source_url


def test_normalize_spreadsheet_no_sheets() -> None:
    sp = {
        "spreadsheetId": "empty_sp",
        "properties": {"title": "Empty"},
        "sheets": [],
    }
    doc = normalize_spreadsheet(sp, CONNECTOR_ID, TENANT_ID)
    assert doc.content == "(no sheets)"
    assert doc.metadata["sheet_count"] == 0


def test_normalize_spreadsheet_metadata() -> None:
    doc = normalize_spreadsheet(SAMPLE_SPREADSHEET, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["spreadsheet_id"] == SAMPLE_SPREADSHEET["spreadsheetId"]
    assert "Sheet1" in doc.metadata["sheet_names"]
    assert doc.metadata["sheet_count"] == 2


def test_normalize_spreadsheet_missing_title() -> None:
    sp = {"spreadsheetId": "sp_no_title", "sheets": []}
    doc = normalize_spreadsheet(sp, CONNECTOR_ID, TENANT_ID)
    # Falls back to spreadsheet ID
    assert doc.title == "sp_no_title"


# ── with_retry() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await with_retry(fn, max_attempts=3)
    assert result == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_transient_error() -> None:
    from exceptions import GoogleSheetsNetworkError
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise GoogleSheetsNetworkError("transient")
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise GoogleSheetsAuthError("Unauthorized", 401)

    with pytest.raises(GoogleSheetsAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_exhausted_raises_last_error() -> None:
    from exceptions import GoogleSheetsNetworkError

    async def fn() -> None:
        raise GoogleSheetsNetworkError("always fails")

    with pytest.raises(GoogleSheetsNetworkError, match="always fails"):
        await with_retry(fn, max_attempts=2, base_delay=0.0)


@pytest.mark.asyncio
async def test_with_retry_rate_limit_respects_retry_after() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GoogleSheetsRateLimitError("Rate limited", retry_after=0.0)
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls == 2


# ── Exception hierarchy ──────────────────────────────────────────────────────


def test_exception_hierarchy() -> None:
    from exceptions import GoogleSheetsError
    assert issubclass(GoogleSheetsAuthError, GoogleSheetsError)
    assert issubclass(GoogleSheetsNetworkError, GoogleSheetsError)
    assert issubclass(GoogleSheetsRateLimitError, GoogleSheetsError)
    assert issubclass(GoogleSheetsNotFoundError, GoogleSheetsError)


def test_auth_error_attributes() -> None:
    exc = GoogleSheetsAuthError("Unauthorized", 401, "invalid_token")
    assert exc.status_code == 401
    assert exc.code == "invalid_token"
    assert str(exc) == "Unauthorized"


def test_rate_limit_error_retry_after() -> None:
    exc = GoogleSheetsRateLimitError("Too many requests", retry_after=30.0)
    assert exc.retry_after == 30.0
    assert exc.status_code == 429
    assert exc.code == "rate_limit"


def test_not_found_error_message() -> None:
    exc = GoogleSheetsNotFoundError("spreadsheet", "sp_id_123")
    assert "sp_id_123" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "not_found"


def test_network_error_attributes() -> None:
    exc = GoogleSheetsNetworkError("Connection reset", status_code=503)
    assert exc.status_code == 503
    assert "Connection reset" in str(exc)


# ── Connector model tests ────────────────────────────────────────────────────


def test_connector_has_correct_type() -> None:
    c = GoogleSheetsConnector()
    assert c.CONNECTOR_TYPE == "google_sheets"
    assert c.AUTH_TYPE == "oauth2"


def test_connector_config_parsing() -> None:
    c = GoogleSheetsConnector(
        tenant_id="t1",
        connector_id="c1",
        config={
            "client_id": "cid",
            "client_secret": "csec",
            "redirect_uri": "https://example.com/callback",
            "access_token": "tok",
        },
    )
    assert c._client_id == "cid"
    assert c._client_secret == "csec"
    assert c._redirect_uri == "https://example.com/callback"
    assert c._access_token == "tok"
    assert c._tenant_id == "t1"
    assert c.connector_id == "c1"


def test_connector_empty_config_defaults() -> None:
    c = GoogleSheetsConnector()
    assert c._client_id == ""
    assert c._client_secret == ""
    assert c._access_token == ""
    assert c.http_client is None


@pytest.mark.asyncio
async def test_connector_context_manager() -> None:
    c = GoogleSheetsConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": VALID_TOKEN,
        }
    )
    async with c as conn:
        assert conn is c
    # After exit, http_client should be None (closed)
    assert c.http_client is None


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    c = GoogleSheetsConnector()
    await c.aclose()  # Should not raise even with no client
    await c.aclose()  # Second close should also be safe


# ── ConnectorDocument model ──────────────────────────────────────────────────


def test_connector_document_defaults() -> None:
    doc = ConnectorDocument(
        source_id="sid",
        title="My Title",
        content="Some content",
        connector_id="conn1",
        tenant_id="ten1",
    )
    assert doc.source_url == ""
    assert doc.metadata == {}


def test_connector_document_metadata_isolation() -> None:
    """Each document must have its own metadata dict, not a shared default."""
    doc1 = ConnectorDocument("id1", "T1", "C1", "conn1", "ten1")
    doc2 = ConnectorDocument("id2", "T2", "C2", "conn1", "ten1")
    doc1.metadata["key"] = "val"
    assert "key" not in doc2.metadata
