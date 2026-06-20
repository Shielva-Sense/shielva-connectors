"""Tests for the Smartsheet connector — no live API calls."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    SmartsheetAuthError,
    SmartsheetError,
    SmartsheetNetworkError,
    SmartsheetNotFoundError,
    SmartsheetRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
    ResourceType,
    SmartsheetUser,
    Sheet,
    Workspace,
    Report,
    SheetRow,
    SheetColumn,
)
from helpers.utils import (
    normalize_sheet,
    normalize_row,
    normalize_workspace,
    normalize_report,
    with_retry,
)
from client.http_client import SmartsheetHTTPClient
from connector import SmartsheetConnector, CONNECTOR_TYPE, AUTH_TYPE

TENANT = "test-tenant"
CONNECTOR_ID = "smartsheet_test"
API_TOKEN = "test_smartsheet_token_12345"


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_sheet(
    sheet_id: int = 123456789,
    name: str = "Project Tracker",
    permalink: str = "https://app.smartsheet.com/sheets/abc",
    access_level: str = "OWNER",
    total_row_count: int = 10,
    created_at: str = "2026-01-01T00:00:00Z",
    modified_at: str = "2026-06-01T00:00:00Z",
) -> Dict[str, Any]:
    return {
        "id": sheet_id,
        "name": name,
        "permalink": permalink,
        "accessLevel": access_level,
        "totalRowCount": total_row_count,
        "createdAt": created_at,
        "modifiedAt": modified_at,
    }


def _make_row(
    row_id: int = 9876543210,
    row_number: int = 1,
    cells: List[Dict[str, Any]] | None = None,
    created_at: str = "2026-01-15T00:00:00Z",
    modified_at: str = "2026-06-10T00:00:00Z",
) -> Dict[str, Any]:
    return {
        "id": row_id,
        "rowNumber": row_number,
        "cells": cells or [
            {"columnId": 111, "displayValue": "Task One", "value": "Task One"},
            {"columnId": 222, "displayValue": "In Progress", "value": "In Progress"},
        ],
        "createdAt": created_at,
        "modifiedAt": modified_at,
    }


def _make_workspace(
    workspace_id: int = 555000,
    name: str = "Engineering Workspace",
    access_level: str = "OWNER",
) -> Dict[str, Any]:
    return {
        "id": workspace_id,
        "name": name,
        "accessLevel": access_level,
    }


def _make_report(
    report_id: int = 777000,
    name: str = "Q2 Summary Report",
    access_level: str = "VIEWER",
    created_at: str = "2026-03-01T00:00:00Z",
    modified_at: str = "2026-06-15T00:00:00Z",
) -> Dict[str, Any]:
    return {
        "id": report_id,
        "name": name,
        "accessLevel": access_level,
        "createdAt": created_at,
        "modifiedAt": modified_at,
    }


def _make_connector(config: Dict[str, Any] | None = None) -> SmartsheetConnector:
    return SmartsheetConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config=config or {"api_token": API_TOKEN},
    )


def _make_aiohttp_mock(status: int = 200, body: Dict[str, Any] | None = None):
    """Build a properly nested aiohttp.ClientSession async-context-manager mock."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=body or {})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_get_ctx = MagicMock()
    mock_get_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_get_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_get_ctx)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session


# ── Exception tests ───────────────────────────────────────────────────────────

class TestExceptions:
    def test_smartsheet_error_is_exception(self) -> None:
        exc = SmartsheetError("base error")
        assert isinstance(exc, Exception)
        assert str(exc) == "base error"

    def test_auth_error_inherits_smartsheet_error(self) -> None:
        exc = SmartsheetAuthError("bad token")
        assert isinstance(exc, SmartsheetError)
        assert isinstance(exc, Exception)

    def test_network_error_inherits_smartsheet_error(self) -> None:
        exc = SmartsheetNetworkError("timeout")
        assert isinstance(exc, SmartsheetError)

    def test_rate_limit_error_inherits_smartsheet_error(self) -> None:
        exc = SmartsheetRateLimitError("too many requests")
        assert isinstance(exc, SmartsheetError)

    def test_not_found_error_inherits_smartsheet_error(self) -> None:
        exc = SmartsheetNotFoundError("sheet not found")
        assert isinstance(exc, SmartsheetError)

    def test_all_exceptions_carry_messages(self) -> None:
        errors = [
            SmartsheetError("a"),
            SmartsheetAuthError("b"),
            SmartsheetNetworkError("c"),
            SmartsheetRateLimitError("d"),
            SmartsheetNotFoundError("e"),
        ]
        for exc in errors:
            assert str(exc) != ""

    def test_exceptions_can_be_raised_and_caught(self) -> None:
        with pytest.raises(SmartsheetAuthError):
            raise SmartsheetAuthError("invalid")

    def test_base_catches_subclasses(self) -> None:
        with pytest.raises(SmartsheetError):
            raise SmartsheetRateLimitError("rate limit")


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

    def test_resource_type_values(self) -> None:
        assert ResourceType.SHEET == "smartsheet_sheet"
        assert ResourceType.ROW == "smartsheet_row"
        assert ResourceType.WORKSPACE == "smartsheet_workspace"
        assert ResourceType.REPORT == "smartsheet_report"

    def test_install_result_healthy(self) -> None:
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
        assert "api_token" in result.message

    def test_health_check_result(self) -> None:
        result = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Connected as: Jane Doe",
        )
        assert result.health == ConnectorHealth.HEALTHY
        assert "Jane" in result.message

    def test_sync_result_defaults(self) -> None:
        result = SyncResult(status=SyncStatus.COMPLETED)
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.documents_failed == 0
        assert result.message == ""

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(id="abc", title="T", content="C")
        assert doc.type == "smartsheet_sheet"
        assert doc.metadata == {}

    def test_smartsheet_user_display_name(self) -> None:
        user = SmartsheetUser(id=1, email="jane@acme.com", first_name="Jane", last_name="Doe")
        assert user.display_name == "Jane Doe"

    def test_smartsheet_user_display_name_falls_back_to_email(self) -> None:
        user = SmartsheetUser(id=1, email="jane@acme.com")
        assert user.display_name == "jane@acme.com"


# ── normalize_sheet tests ─────────────────────────────────────────────────────

class TestNormalizeSheet:
    def _expected_id(self, sheet_id: str) -> str:
        return hashlib.sha256(f"sheet:{sheet_id}".encode()).hexdigest()[:16]

    def test_stable_id_from_sheet_id(self) -> None:
        sheet = _make_sheet(sheet_id=123456789)
        doc = normalize_sheet(sheet)
        assert doc.id == self._expected_id("123456789")

    def test_id_is_16_hex_chars(self) -> None:
        sheet = _make_sheet(sheet_id=999)
        doc = normalize_sheet(sheet)
        assert len(doc.id) == 16
        assert all(c in "0123456789abcdef" for c in doc.id)

    def test_id_differs_for_different_sheets(self) -> None:
        doc1 = normalize_sheet(_make_sheet(sheet_id=111))
        doc2 = normalize_sheet(_make_sheet(sheet_id=222))
        assert doc1.id != doc2.id

    def test_type_is_smartsheet_sheet(self) -> None:
        doc = normalize_sheet(_make_sheet())
        assert doc.type == "smartsheet_sheet"

    def test_title_includes_sheet_name(self) -> None:
        doc = normalize_sheet(_make_sheet(name="Project Tracker"))
        assert "Project Tracker" in doc.title

    def test_content_includes_access_level(self) -> None:
        doc = normalize_sheet(_make_sheet(access_level="OWNER"))
        assert "OWNER" in doc.content

    def test_content_includes_total_row_count(self) -> None:
        doc = normalize_sheet(_make_sheet(total_row_count=42))
        assert "42" in doc.content

    def test_content_includes_permalink(self) -> None:
        doc = normalize_sheet(_make_sheet(permalink="https://app.smartsheet.com/sheets/xyz"))
        assert "https://app.smartsheet.com/sheets/xyz" in doc.content

    def test_metadata_has_required_fields(self) -> None:
        sheet = _make_sheet(sheet_id=123, name="Test Sheet", access_level="EDITOR")
        doc = normalize_sheet(sheet)
        assert doc.metadata["sheet_id"] == "123"
        assert doc.metadata["name"] == "Test Sheet"
        assert doc.metadata["access_level"] == "EDITOR"
        assert doc.metadata["source"] == "smartsheet"

    def test_handles_missing_optional_fields(self) -> None:
        sheet = {"id": 100, "name": "Minimal Sheet"}
        doc = normalize_sheet(sheet)
        assert isinstance(doc, ConnectorDocument)
        assert doc.metadata["total_row_count"] == 0


# ── normalize_row tests ───────────────────────────────────────────────────────

class TestNormalizeRow:
    def _expected_id(self, row_id: str) -> str:
        return hashlib.sha256(f"row:{row_id}".encode()).hexdigest()[:16]

    def test_stable_id_from_row_id(self) -> None:
        row = _make_row(row_id=9876543210)
        doc = normalize_row(row, sheet_id=123456789)
        assert doc.id == self._expected_id("9876543210")

    def test_id_is_16_hex_chars(self) -> None:
        row = _make_row(row_id=111)
        doc = normalize_row(row, sheet_id=999)
        assert len(doc.id) == 16

    def test_id_differs_for_different_rows(self) -> None:
        doc1 = normalize_row(_make_row(row_id=111), sheet_id=1)
        doc2 = normalize_row(_make_row(row_id=222), sheet_id=1)
        assert doc1.id != doc2.id

    def test_type_is_smartsheet_row(self) -> None:
        doc = normalize_row(_make_row(), sheet_id=123)
        assert doc.type == "smartsheet_row"

    def test_title_includes_row_number(self) -> None:
        doc = normalize_row(_make_row(row_number=5), sheet_id=999)
        assert "5" in doc.title

    def test_title_includes_sheet_id(self) -> None:
        doc = normalize_row(_make_row(), sheet_id=888)
        assert "888" in doc.title

    def test_content_includes_cell_display_values(self) -> None:
        row = _make_row(
            cells=[{"columnId": 111, "displayValue": "My Task", "value": "My Task"}]
        )
        doc = normalize_row(row, sheet_id=123)
        assert "My Task" in doc.content

    def test_content_includes_cells_json(self) -> None:
        cells = [{"columnId": 111, "displayValue": "Alpha", "value": "Alpha"}]
        row = _make_row(cells=cells)
        doc = normalize_row(row, sheet_id=123)
        assert "Cells JSON:" in doc.content
        assert json.loads(doc.content.split("Cells JSON: ")[1]) == cells

    def test_metadata_has_required_fields(self) -> None:
        row = _make_row(row_id=555, row_number=3)
        doc = normalize_row(row, sheet_id=999)
        assert doc.metadata["row_id"] == "555"
        assert doc.metadata["row_number"] == 3
        assert doc.metadata["sheet_id"] == "999"
        assert doc.metadata["source"] == "smartsheet"

    def test_metadata_includes_cells(self) -> None:
        cells = [{"columnId": 1, "displayValue": "X", "value": "X"}]
        row = _make_row(cells=cells)
        doc = normalize_row(row, sheet_id=1)
        assert doc.metadata["cells"] == cells

    def test_handles_empty_cells(self) -> None:
        row = _make_row(cells=[])
        doc = normalize_row(row, sheet_id=1)
        assert isinstance(doc, ConnectorDocument)

    def test_handles_none_cells(self) -> None:
        row = {"id": 100, "rowNumber": 1, "cells": None}
        doc = normalize_row(row, sheet_id=1)
        assert isinstance(doc, ConnectorDocument)


# ── normalize_workspace tests ─────────────────────────────────────────────────

class TestNormalizeWorkspace:
    def _expected_id(self, ws_id: str) -> str:
        return hashlib.sha256(f"workspace:{ws_id}".encode()).hexdigest()[:16]

    def test_stable_id_from_workspace_id(self) -> None:
        ws = _make_workspace(workspace_id=555000)
        doc = normalize_workspace(ws)
        assert doc.id == self._expected_id("555000")

    def test_id_is_16_hex_chars(self) -> None:
        doc = normalize_workspace(_make_workspace(workspace_id=1))
        assert len(doc.id) == 16

    def test_type_is_smartsheet_workspace(self) -> None:
        doc = normalize_workspace(_make_workspace())
        assert doc.type == "smartsheet_workspace"

    def test_title_includes_workspace_name(self) -> None:
        doc = normalize_workspace(_make_workspace(name="Engineering Workspace"))
        assert "Engineering Workspace" in doc.title

    def test_content_includes_access_level(self) -> None:
        doc = normalize_workspace(_make_workspace(access_level="ADMIN"))
        assert "ADMIN" in doc.content

    def test_metadata_has_required_fields(self) -> None:
        ws = _make_workspace(workspace_id=999, name="My WS", access_level="VIEWER")
        doc = normalize_workspace(ws)
        assert doc.metadata["workspace_id"] == "999"
        assert doc.metadata["name"] == "My WS"
        assert doc.metadata["access_level"] == "VIEWER"
        assert doc.metadata["source"] == "smartsheet"


# ── normalize_report tests ────────────────────────────────────────────────────

class TestNormalizeReport:
    def _expected_id(self, report_id: str) -> str:
        return hashlib.sha256(f"report:{report_id}".encode()).hexdigest()[:16]

    def test_stable_id_from_report_id(self) -> None:
        rpt = _make_report(report_id=777000)
        doc = normalize_report(rpt)
        assert doc.id == self._expected_id("777000")

    def test_id_is_16_hex_chars(self) -> None:
        doc = normalize_report(_make_report(report_id=42))
        assert len(doc.id) == 16

    def test_type_is_smartsheet_report(self) -> None:
        doc = normalize_report(_make_report())
        assert doc.type == "smartsheet_report"

    def test_title_includes_report_name(self) -> None:
        doc = normalize_report(_make_report(name="Q2 Summary Report"))
        assert "Q2 Summary Report" in doc.title

    def test_content_includes_access_level(self) -> None:
        doc = normalize_report(_make_report(access_level="VIEWER"))
        assert "VIEWER" in doc.content

    def test_content_includes_dates(self) -> None:
        doc = normalize_report(_make_report(created_at="2026-03-01T00:00:00Z"))
        assert "2026-03-01" in doc.content

    def test_metadata_has_required_fields(self) -> None:
        rpt = _make_report(report_id=321, name="Monthly Report", access_level="OWNER")
        doc = normalize_report(rpt)
        assert doc.metadata["report_id"] == "321"
        assert doc.metadata["name"] == "Monthly Report"
        assert doc.metadata["access_level"] == "OWNER"
        assert doc.metadata["source"] == "smartsheet"


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
    async def test_retries_on_smartsheet_error(self) -> None:
        calls = []

        async def fn() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise SmartsheetError("transient")
            return "recovered"

        result = await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert result == "recovered"
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_auth_error(self) -> None:
        calls = []

        async def fn() -> str:
            calls.append(1)
            raise SmartsheetAuthError("bad token")

        with pytest.raises(SmartsheetAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self) -> None:
        async def fn() -> str:
            raise SmartsheetNetworkError("timeout")

        with pytest.raises(SmartsheetNetworkError):
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
                raise SmartsheetNetworkError("timeout")
            return "ok"

        result = await with_retry(fn, max_attempts=2, base_delay=0.01)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self) -> None:
        calls = []

        async def fn() -> str:
            calls.append(1)
            if len(calls) < 2:
                raise SmartsheetRateLimitError("rate limited")
            return "ok"

        result = await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_generic_exception(self) -> None:
        calls = []

        async def fn() -> str:
            calls.append(1)
            if len(calls) < 2:
                raise ValueError("unexpected")
            return "ok"

        result = await with_retry(fn, max_attempts=2, base_delay=0.01)
        assert result == "ok"


# ── HTTP client tests ─────────────────────────────────────────────────────────

class TestSmartsheetHTTPClient:
    def _client(self) -> SmartsheetHTTPClient:
        return SmartsheetHTTPClient(
            config={"api_token": API_TOKEN},
            base_url="https://api.smartsheet.com/2.0",
        )

    def test_bearer_header_includes_token(self) -> None:
        client = self._client()
        headers = client._build_headers()
        assert headers["Authorization"] == f"Bearer {API_TOKEN}"
        assert headers["Content-Type"] == "application/json"

    def test_bearer_prefix_is_present(self) -> None:
        client = self._client()
        headers = client._build_headers()
        assert headers["Authorization"].startswith("Bearer ")

    def test_raise_for_status_ok(self) -> None:
        client = self._client()
        # Should not raise
        client._raise_for_status(200, {})
        client._raise_for_status(201, {})

    def test_raise_for_status_401_raises_auth_error(self) -> None:
        client = self._client()
        with pytest.raises(SmartsheetAuthError):
            client._raise_for_status(401, {"message": "Unauthorized", "errorCode": 1004})

    def test_raise_for_status_403_raises_auth_error(self) -> None:
        client = self._client()
        with pytest.raises(SmartsheetAuthError):
            client._raise_for_status(403, {"message": "Forbidden"})

    def test_raise_for_status_404_raises_not_found(self) -> None:
        client = self._client()
        with pytest.raises(SmartsheetNotFoundError):
            client._raise_for_status(404, {"message": "Not found", "errorCode": 1006})

    def test_raise_for_status_429_raises_rate_limit(self) -> None:
        client = self._client()
        with pytest.raises(SmartsheetRateLimitError):
            client._raise_for_status(429, {"message": "Rate limit exceeded"})

    def test_raise_for_status_400_raises_smartsheet_error(self) -> None:
        client = self._client()
        with pytest.raises(SmartsheetError):
            client._raise_for_status(400, {"message": "Bad request", "errorCode": 1008})

    def test_raise_for_status_500_raises_network_error(self) -> None:
        client = self._client()
        with pytest.raises(SmartsheetNetworkError):
            client._raise_for_status(500, {"message": "Internal server error"})

    def test_raise_for_status_503_raises_network_error(self) -> None:
        client = self._client()
        with pytest.raises(SmartsheetNetworkError):
            client._raise_for_status(503, {"message": "Service unavailable"})

    @pytest.mark.asyncio
    async def test_get_current_user_returns_user(self) -> None:
        client = self._client()
        body = {
            "id": 99,
            "email": "jane@acme.com",
            "firstName": "Jane",
            "lastName": "Doe",
        }
        mock_session = _make_aiohttp_mock(status=200, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_current_user()
            assert result["email"] == "jane@acme.com"
            assert result["firstName"] == "Jane"

    @pytest.mark.asyncio
    async def test_get_current_user_raises_auth_on_401(self) -> None:
        client = self._client()
        body = {"message": "Unauthorized", "errorCode": 1004}
        mock_session = _make_aiohttp_mock(status=401, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(SmartsheetAuthError):
                await client.get_current_user()

    @pytest.mark.asyncio
    async def test_get_sheets_returns_data(self) -> None:
        client = self._client()
        body = {
            "data": [_make_sheet(123), _make_sheet(456, name="Sheet 2")],
            "totalPages": 1,
            "pageNumber": 1,
            "totalCount": 2,
            "pageSize": 100,
        }
        mock_session = _make_aiohttp_mock(status=200, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_sheets()
            assert len(result["data"]) == 2
            assert result["totalPages"] == 1

    @pytest.mark.asyncio
    async def test_get_sheets_passes_page_params(self) -> None:
        client = self._client()
        body = {"data": [], "totalPages": 1, "pageNumber": 2}
        mock_session = _make_aiohttp_mock(status=200, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_sheets(page=2, page_size=50)
            assert result["pageNumber"] == 2

    @pytest.mark.asyncio
    async def test_get_sheet_single_sheet(self) -> None:
        client = self._client()
        body = {
            "id": 123,
            "name": "Project Tracker",
            "columns": [{"id": 1, "title": "Task", "type": "TEXT_NUMBER"}],
            "rows": [_make_row()],
        }
        mock_session = _make_aiohttp_mock(status=200, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_sheet(123)
            assert result["id"] == 123
            assert result["name"] == "Project Tracker"

    @pytest.mark.asyncio
    async def test_get_sheet_raises_not_found_on_404(self) -> None:
        client = self._client()
        body = {"message": "Sheet not found", "errorCode": 1006}
        mock_session = _make_aiohttp_mock(status=404, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(SmartsheetNotFoundError):
                await client.get_sheet(99999)

    @pytest.mark.asyncio
    async def test_get_rows_returns_data(self) -> None:
        client = self._client()
        body = {
            "data": [_make_row(1, 1), _make_row(2, 2)],
            "totalPages": 1,
            "pageNumber": 1,
        }
        mock_session = _make_aiohttp_mock(status=200, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_rows(sheet_id=123)
            assert len(result["data"]) == 2

    @pytest.mark.asyncio
    async def test_get_workspaces_returns_data(self) -> None:
        client = self._client()
        body = {
            "data": [
                _make_workspace(1, "WS One"),
                _make_workspace(2, "WS Two"),
            ]
        }
        mock_session = _make_aiohttp_mock(status=200, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_workspaces()
            assert len(result["data"]) == 2
            assert result["data"][0]["name"] == "WS One"

    @pytest.mark.asyncio
    async def test_get_reports_returns_data(self) -> None:
        client = self._client()
        body = {
            "data": [_make_report(1, "Report A"), _make_report(2, "Report B")],
            "totalPages": 1,
            "pageNumber": 1,
        }
        mock_session = _make_aiohttp_mock(status=200, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_reports()
            assert len(result["data"]) == 2

    @pytest.mark.asyncio
    async def test_get_raises_network_error_on_client_error(self) -> None:
        import aiohttp as _aiohttp
        client = self._client()
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(side_effect=_aiohttp.ClientError("conn refused"))
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(SmartsheetNetworkError):
                await client.get_current_user()

    @pytest.mark.asyncio
    async def test_get_reports_total_pages_pagination(self) -> None:
        client = self._client()
        body = {
            "data": [_make_report(1, "R1")],
            "totalPages": 3,
            "pageNumber": 1,
        }
        mock_session = _make_aiohttp_mock(status=200, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_reports(page=1)
            assert result["totalPages"] == 3

    @pytest.mark.asyncio
    async def test_get_folders_returns_data(self) -> None:
        client = self._client()
        body = {
            "data": [
                {"id": 1, "name": "Projects"},
                {"id": 2, "name": "Archive"},
            ]
        }
        mock_session = _make_aiohttp_mock(status=200, body=body)
        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_folders()
            assert len(result["data"]) == 2


# ── Install tests ─────────────────────────────────────────────────────────────

class TestSmartsheetConnectorInstall:
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
        c = SmartsheetConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={})
        result = await c.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_token" in result.message

    @pytest.mark.asyncio
    async def test_install_fails_empty_token(self) -> None:
        c = SmartsheetConnector(
            tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"api_token": ""}
        )
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE

    @pytest.mark.asyncio
    async def test_install_message_present_on_success(self) -> None:
        c = _make_connector()
        result = await c.install()
        assert result.message != ""


# ── Health check tests ────────────────────────────────────────────────────────

class TestSmartsheetConnectorHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_ok(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_current_user=AsyncMock(
                    return_value={
                        "id": 1,
                        "email": "jane@acme.com",
                        "firstName": "Jane",
                        "lastName": "Doe",
                    }
                )
            )
        )
        result = await c.health_check()
        assert isinstance(result, HealthCheckResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Jane Doe" in result.message

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_current_user=AsyncMock(
                    side_effect=SmartsheetAuthError("invalid token")
                )
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
                get_current_user=AsyncMock(
                    side_effect=SmartsheetNetworkError("timeout")
                )
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
                get_current_user=AsyncMock(
                    return_value={"id": 1, "email": "noop@example.com", "firstName": "", "lastName": ""}
                )
            )
        )
        result = await c.health_check()
        assert "noop@example.com" in result.message

    @pytest.mark.asyncio
    async def test_health_check_generic_exception(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_current_user=AsyncMock(side_effect=RuntimeError("unexpected"))
            )
        )
        result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ── Connector init and module-level tests ─────────────────────────────────────

class TestSmartsheetConnectorInit:
    def test_defaults(self) -> None:
        c = SmartsheetConnector()
        assert c.tenant_id == ""
        assert c.connector_id == ""
        assert c.config == {}

    def test_with_config(self) -> None:
        c = _make_connector({"api_token": "mytoken"})
        assert c.config["api_token"] == "mytoken"
        assert c.tenant_id == TENANT
        assert c.connector_id == CONNECTOR_ID

    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "smartsheet"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_type_attr(self) -> None:
        c = _make_connector()
        assert c.CONNECTOR_TYPE == "smartsheet"

    def test_auth_type_attr(self) -> None:
        c = _make_connector()
        assert c.AUTH_TYPE == "api_key"

    def test_get_token(self) -> None:
        c = _make_connector({"api_token": "tok_abc"})
        assert c._get_token() == "tok_abc"

    def test_get_token_missing(self) -> None:
        c = SmartsheetConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={})
        assert c._get_token() == ""

    def test_ensure_client_creates_on_demand(self) -> None:
        c = _make_connector()
        assert c._http_client is None
        client = c._ensure_client()
        assert isinstance(client, SmartsheetHTTPClient)
        assert c._http_client is client

    def test_ensure_client_returns_same_instance(self) -> None:
        c = _make_connector()
        client1 = c._ensure_client()
        client2 = c._ensure_client()
        assert client1 is client2


# ── list_sheets tests ─────────────────────────────────────────────────────────

class TestListSheets:
    @pytest.mark.asyncio
    async def test_list_sheets_single_page(self) -> None:
        c = _make_connector()
        response = {
            "data": [_make_sheet(1), _make_sheet(2)],
            "totalPages": 1,
            "pageNumber": 1,
        }
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_sheets=AsyncMock(return_value=response))
        )
        result = await c.list_sheets()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_sheets_paginates_across_total_pages(self) -> None:
        c = _make_connector()
        page1_response = {
            "data": [_make_sheet(i) for i in range(100)],
            "totalPages": 2,
            "pageNumber": 1,
        }
        page2_response = {
            "data": [_make_sheet(i) for i in range(100, 130)],
            "totalPages": 2,
            "pageNumber": 2,
        }
        call_count = [0]

        async def mock_get_sheets(page=1, page_size=100):
            call_count[0] += 1
            if page == 1:
                return page1_response
            return page2_response

        c._ensure_client = MagicMock(
            return_value=MagicMock(get_sheets=mock_get_sheets)
        )
        result = await c.list_sheets()
        assert len(result) == 130
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_list_sheets_returns_empty(self) -> None:
        c = _make_connector()
        response = {"data": [], "totalPages": 1, "pageNumber": 1}
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_sheets=AsyncMock(return_value=response))
        )
        result = await c.list_sheets()
        assert result == []


# ── list_rows tests ───────────────────────────────────────────────────────────

class TestListRows:
    @pytest.mark.asyncio
    async def test_list_rows_single_page(self) -> None:
        c = _make_connector()
        response = {
            "data": [_make_row(1, 1), _make_row(2, 2)],
            "totalPages": 1,
            "pageNumber": 1,
        }
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_rows=AsyncMock(return_value=response))
        )
        result = await c.list_rows(sheet_id=123)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_rows_paginates(self) -> None:
        c = _make_connector()
        page1_response = {
            "data": [_make_row(i, i) for i in range(500)],
            "totalPages": 2,
            "pageNumber": 1,
        }
        page2_response = {
            "data": [_make_row(i, i) for i in range(500, 600)],
            "totalPages": 2,
            "pageNumber": 2,
        }

        async def mock_get_rows(sheet_id, page=1, page_size=500):
            if page == 1:
                return page1_response
            return page2_response

        c._ensure_client = MagicMock(
            return_value=MagicMock(get_rows=mock_get_rows)
        )
        result = await c.list_rows(sheet_id=123)
        assert len(result) == 600

    @pytest.mark.asyncio
    async def test_list_rows_returns_empty(self) -> None:
        c = _make_connector()
        response = {"data": [], "totalPages": 1, "pageNumber": 1}
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_rows=AsyncMock(return_value=response))
        )
        result = await c.list_rows(sheet_id=123)
        assert result == []


# ── list_workspaces tests ─────────────────────────────────────────────────────

class TestListWorkspaces:
    @pytest.mark.asyncio
    async def test_list_workspaces_returns_list(self) -> None:
        c = _make_connector()
        response = {
            "data": [_make_workspace(1, "WS1"), _make_workspace(2, "WS2")]
        }
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_workspaces=AsyncMock(return_value=response))
        )
        result = await c.list_workspaces()
        assert len(result) == 2
        assert result[0]["name"] == "WS1"

    @pytest.mark.asyncio
    async def test_list_workspaces_returns_empty(self) -> None:
        c = _make_connector()
        response = {"data": []}
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_workspaces=AsyncMock(return_value=response))
        )
        result = await c.list_workspaces()
        assert result == []


# ── list_reports tests ────────────────────────────────────────────────────────

class TestListReports:
    @pytest.mark.asyncio
    async def test_list_reports_returns_list(self) -> None:
        c = _make_connector()
        response = {
            "data": [_make_report(1, "R1"), _make_report(2, "R2")],
            "totalPages": 1,
            "pageNumber": 1,
        }
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_reports=AsyncMock(return_value=response))
        )
        result = await c.list_reports()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_reports_returns_empty(self) -> None:
        c = _make_connector()
        response = {"data": [], "totalPages": 1, "pageNumber": 1}
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_reports=AsyncMock(return_value=response))
        )
        result = await c.list_reports()
        assert result == []


# ── get_sheet tests ───────────────────────────────────────────────────────────

class TestGetSheet:
    @pytest.mark.asyncio
    async def test_get_sheet_returns_sheet(self) -> None:
        c = _make_connector()
        sheet = {
            "id": 123,
            "name": "Project Tracker",
            "columns": [],
            "rows": [_make_row()],
        }
        c._ensure_client = MagicMock(
            return_value=MagicMock(get_sheet=AsyncMock(return_value=sheet))
        )
        result = await c.get_sheet(123)
        assert result["id"] == 123
        assert result["name"] == "Project Tracker"

    @pytest.mark.asyncio
    async def test_get_sheet_propagates_not_found(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_sheet=AsyncMock(side_effect=SmartsheetNotFoundError("not found"))
            )
        )
        with pytest.raises(SmartsheetNotFoundError):
            await c.get_sheet(99999)

    @pytest.mark.asyncio
    async def test_get_sheet_propagates_auth_error(self) -> None:
        c = _make_connector()
        c._ensure_client = MagicMock(
            return_value=MagicMock(
                get_sheet=AsyncMock(side_effect=SmartsheetAuthError("bad token"))
            )
        )
        with pytest.raises(SmartsheetAuthError):
            await c.get_sheet(123)


# ── Sync tests ────────────────────────────────────────────────────────────────

class TestSmartsheetConnectorSync:
    @pytest.mark.asyncio
    async def test_sync_success_sheets_and_rows(self) -> None:
        c = _make_connector()
        sheets = [_make_sheet(1, "Sheet A"), _make_sheet(2, "Sheet B")]
        rows = [_make_row(10, 1), _make_row(11, 2)]
        workspaces = [_make_workspace(1)]
        reports: List[Dict[str, Any]] = []

        c.list_sheets = AsyncMock(return_value=sheets)
        c.list_rows = AsyncMock(return_value=rows)
        c.list_workspaces = AsyncMock(return_value=workspaces)
        c.list_reports = AsyncMock(return_value=reports)

        result = await c.sync()
        assert isinstance(result, SyncResult)
        assert result.status == SyncStatus.COMPLETED
        # 2 sheets + 2*2 rows + 1 workspace = 7 found, 7 synced
        assert result.documents_found == 7
        assert result.documents_synced == 7
        assert result.documents_failed == 0

    @pytest.mark.asyncio
    async def test_sync_partial_on_row_failure(self) -> None:
        c = _make_connector()
        sheets = [_make_sheet(1)]

        async def mock_list_rows(sheet_id, **kwargs):
            raise SmartsheetError("rows fetch failed")

        c.list_sheets = AsyncMock(return_value=sheets)
        c.list_rows = mock_list_rows
        c.list_workspaces = AsyncMock(return_value=[])
        c.list_reports = AsyncMock(return_value=[])

        result = await c.sync()
        # Sheet itself is normalized ok; rows fail
        assert result.documents_synced >= 1
        assert result.documents_failed >= 1
        assert result.status == SyncStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_sync_failed_on_auth_error(self) -> None:
        c = _make_connector()
        c.list_sheets = AsyncMock(side_effect=SmartsheetAuthError("invalid token"))

        result = await c.sync()
        assert result.status == SyncStatus.FAILED
        assert "invalid token" in result.message

    @pytest.mark.asyncio
    async def test_sync_empty_sheets(self) -> None:
        c = _make_connector()
        c.list_sheets = AsyncMock(return_value=[])
        c.list_workspaces = AsyncMock(return_value=[])
        c.list_reports = AsyncMock(return_value=[])

        result = await c.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    @pytest.mark.asyncio
    async def test_sync_skips_sheet_without_id(self) -> None:
        c = _make_connector()
        sheets = [{"id": None, "name": "Ghost"}, _make_sheet(1)]
        c.list_sheets = AsyncMock(return_value=sheets)
        c.list_rows = AsyncMock(return_value=[_make_row(10, 1)])
        c.list_workspaces = AsyncMock(return_value=[])
        c.list_reports = AsyncMock(return_value=[])

        result = await c.sync()
        # Only 1 sheet processed (no id skipped), 1 sheet + 1 row
        assert result.documents_found >= 1

    @pytest.mark.asyncio
    async def test_sync_message_format(self) -> None:
        c = _make_connector()
        c.list_sheets = AsyncMock(return_value=[_make_sheet(1)])
        c.list_rows = AsyncMock(return_value=[_make_row(10, 1)])
        c.list_workspaces = AsyncMock(return_value=[])
        c.list_reports = AsyncMock(return_value=[])

        result = await c.sync()
        assert "sheet" in result.message.lower()
        assert "synced" in result.message.lower()

    @pytest.mark.asyncio
    async def test_sync_includes_workspaces(self) -> None:
        c = _make_connector()
        c.list_sheets = AsyncMock(return_value=[])
        c.list_workspaces = AsyncMock(return_value=[_make_workspace(1), _make_workspace(2)])
        c.list_reports = AsyncMock(return_value=[])

        result = await c.sync()
        assert result.documents_found >= 2
        assert result.documents_synced >= 2

    @pytest.mark.asyncio
    async def test_sync_includes_reports(self) -> None:
        c = _make_connector()
        c.list_sheets = AsyncMock(return_value=[])
        c.list_workspaces = AsyncMock(return_value=[])
        c.list_reports = AsyncMock(return_value=[_make_report(1), _make_report(2)])

        result = await c.sync()
        assert result.documents_found >= 2
        assert result.documents_synced >= 2


# ── Connector lifecycle tests ─────────────────────────────────────────────────

class TestSmartsheetConnectorLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_clears_client(self) -> None:
        c = _make_connector()
        c._http_client = SmartsheetHTTPClient(config={"api_token": API_TOKEN})
        await c.aclose()
        assert c._http_client is None

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        async with SmartsheetConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_token": API_TOKEN},
        ) as c:
            assert c.tenant_id == TENANT
        assert c._http_client is None
