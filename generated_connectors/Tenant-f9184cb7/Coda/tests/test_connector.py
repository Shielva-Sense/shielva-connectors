"""Tests for the Coda connector — no live API calls."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    CodaAuthError,
    CodaError,
    CodaNetworkError,
    CodaNotFoundError,
    CodaRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorHealth,
    ConnectorDocument,
    CodaResourceType,
    InstallResult,
    HealthCheckResult,
    SyncResult,
    SyncStatus,
)
from helpers.utils import (
    normalize_doc,
    normalize_page,
    normalize_table,
    normalize_row,
    with_retry,
    _stable_id,
)
from client.http_client import CodaHTTPClient
from connector import CodaConnector

TENANT = "Tenant-f9184cb7"
CONNECTOR_ID = "coda_test"
TOKEN = "test-coda-api-token-abc123"

DOC_ID = "AbCdEfGh"
PAGE_ID = "pAbCdEfGh"
TABLE_ID = "tAbCdEfGh"
ROW_ID = "rAbCdEfGh"


# ── fixture helpers ───────────────────────────────────────────────────────────

def _make_doc(
    doc_id: str = DOC_ID,
    name: str = "My Test Doc",
    browser_link: str = "https://coda.io/d/My-Test-Doc_dAbCdEfGh",
    owner_name: str = "Test User",
) -> Dict[str, Any]:
    return {
        "id": doc_id,
        "type": "doc",
        "href": f"https://coda.io/apis/v1/docs/{doc_id}",
        "browserLink": browser_link,
        "name": name,
        "owner": "test@example.com",
        "ownerName": owner_name,
        "createdAt": "2024-01-01T00:00:00.000Z",
        "updatedAt": "2024-06-01T00:00:00.000Z",
        "folder": {"id": "folder-1", "type": "folder", "href": "...", "browserLink": "..."},
    }


def _make_page(
    page_id: str = PAGE_ID,
    name: str = "My Test Page",
    doc_id: str = DOC_ID,
) -> Dict[str, Any]:
    return {
        "id": page_id,
        "type": "canvas",
        "href": f"https://coda.io/apis/v1/docs/{doc_id}/pages/{page_id}",
        "browserLink": f"https://coda.io/d/_d{doc_id}#{page_id}",
        "name": name,
        "createdAt": "2024-01-01T00:00:00.000Z",
        "updatedAt": "2024-06-01T00:00:00.000Z",
        "parent": {"id": "parent-page-1"},
    }


def _make_table(
    table_id: str = TABLE_ID,
    name: str = "My Test Table",
    doc_id: str = DOC_ID,
    row_count: int = 5,
) -> Dict[str, Any]:
    return {
        "id": table_id,
        "type": "table",
        "tableType": "table",
        "href": f"https://coda.io/apis/v1/docs/{doc_id}/tables/{table_id}",
        "browserLink": f"https://coda.io/d/_d{doc_id}#{table_id}",
        "name": name,
        "rowCount": row_count,
        "createdAt": "2024-01-01T00:00:00.000Z",
        "updatedAt": "2024-06-01T00:00:00.000Z",
        "columns": [
            {"id": "col-a", "name": "Name"},
            {"id": "col-b", "name": "Status"},
            {"id": "col-c", "name": "Due Date"},
        ],
    }


def _make_row(
    row_id: str = ROW_ID,
    name: str = "Row 1",
    doc_id: str = DOC_ID,
    table_id: str = TABLE_ID,
) -> Dict[str, Any]:
    return {
        "id": row_id,
        "type": "row",
        "href": f"https://coda.io/apis/v1/docs/{doc_id}/tables/{table_id}/rows/{row_id}",
        "browserLink": f"https://coda.io/d/_d{doc_id}#{table_id}/{row_id}",
        "name": name,
        "index": 0,
        "createdAt": "2024-01-15T00:00:00.000Z",
        "updatedAt": "2024-05-01T00:00:00.000Z",
        "values": {
            "col-a": "Alice",
            "col-b": "Done",
            "col-c": "2024-03-01",
        },
    }


def _make_whoami(login_id: str = "test@example.com", name: str = "Test User") -> Dict[str, Any]:
    return {
        "loginId": login_id,
        "name": name,
        "type": "person",
    }


def _make_list_response(
    items: List[Dict[str, Any]],
    next_page_token: Optional[str] = None,
) -> Dict[str, Any]:
    resp: Dict[str, Any] = {"items": items}
    if next_page_token:
        resp["nextPageToken"] = next_page_token
        resp["nextPageLink"] = f"https://coda.io/apis/v1/docs?pageToken={next_page_token}"
    return resp


def _make_connector(token: str = TOKEN) -> CodaConnector:
    return CodaConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config={"api_token": token},
    )


def _make_client(token: str = TOKEN) -> CodaHTTPClient:
    return CodaHTTPClient(config={"api_token": token})


# ── exception tests ───────────────────────────────────────────────────────────

class TestExceptions:
    def test_coda_error_is_base_exception(self) -> None:
        exc = CodaError("base error")
        assert isinstance(exc, Exception)
        assert str(exc) == "base error"

    def test_auth_error_is_coda_error(self) -> None:
        exc = CodaAuthError("unauthorized")
        assert isinstance(exc, CodaError)

    def test_network_error_is_coda_error(self) -> None:
        exc = CodaNetworkError("timeout")
        assert isinstance(exc, CodaError)

    def test_rate_limit_error_is_coda_error(self) -> None:
        exc = CodaRateLimitError("429")
        assert isinstance(exc, CodaError)

    def test_not_found_error_is_coda_error(self) -> None:
        exc = CodaNotFoundError("not found")
        assert isinstance(exc, CodaError)

    def test_exception_messages_preserved(self) -> None:
        exc = CodaError("custom message")
        assert "custom message" in str(exc)

    def test_exception_hierarchy_distinct(self) -> None:
        assert CodaAuthError is not CodaNetworkError
        assert CodaRateLimitError is not CodaNotFoundError


# ── models tests ──────────────────────────────────────────────────────────────

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

    def test_coda_resource_type_values(self) -> None:
        assert CodaResourceType.DOC == "doc"
        assert CodaResourceType.PAGE == "page"
        assert CodaResourceType.TABLE == "table"
        assert CodaResourceType.ROW == "row"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(id="abc", title="Test", content="body")
        assert doc.type == "coda_doc"
        assert doc.metadata == {}

    def test_install_result_fields(self) -> None:
        result = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=CONNECTOR_ID,
            message="ok",
        )
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    def test_health_check_result_fields(self) -> None:
        result = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Connected — user: Test User",
        )
        assert "Test User" in result.message

    def test_sync_result_defaults(self) -> None:
        result = SyncResult(status=SyncStatus.COMPLETED)
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.documents_failed == 0
        assert result.message == ""


# ── normalize_doc tests ───────────────────────────────────────────────────────

class TestNormalizeDoc:
    def test_stable_id_from_sha256(self) -> None:
        raw = _make_doc(doc_id=DOC_ID)
        doc = normalize_doc(raw)
        expected = hashlib.sha256(f"doc:{DOC_ID}".encode()).hexdigest()[:16]
        assert doc.id == expected

    def test_same_doc_id_always_same_stable_id(self) -> None:
        raw = _make_doc(doc_id="stable-doc-123")
        doc1 = normalize_doc(raw)
        doc2 = normalize_doc(raw)
        assert doc1.id == doc2.id

    def test_different_doc_ids_produce_different_stable_ids(self) -> None:
        doc1 = normalize_doc(_make_doc(doc_id="doc-aaa"))
        doc2 = normalize_doc(_make_doc(doc_id="doc-bbb"))
        assert doc1.id != doc2.id

    def test_title_extracted(self) -> None:
        raw = _make_doc(name="My Awesome Doc")
        doc = normalize_doc(raw)
        assert doc.title == "My Awesome Doc"

    def test_type_is_coda_doc(self) -> None:
        raw = _make_doc()
        doc = normalize_doc(raw)
        assert doc.type == "coda_doc"

    def test_browser_link_in_content(self) -> None:
        raw = _make_doc(browser_link="https://coda.io/d/My-Doc_dXyz")
        doc = normalize_doc(raw)
        assert "https://coda.io/d/My-Doc_dXyz" in doc.content

    def test_owner_name_in_content(self) -> None:
        raw = _make_doc(owner_name="Alice Smith")
        doc = normalize_doc(raw)
        assert "Alice Smith" in doc.content

    def test_metadata_resource_type(self) -> None:
        raw = _make_doc()
        doc = normalize_doc(raw)
        assert doc.metadata["resource_type"] == "doc"
        assert doc.metadata["source"] == "coda"

    def test_metadata_doc_id(self) -> None:
        raw = _make_doc(doc_id=DOC_ID)
        doc = normalize_doc(raw)
        assert doc.metadata["doc_id"] == DOC_ID

    def test_metadata_folder_id_extracted(self) -> None:
        raw = _make_doc()
        doc = normalize_doc(raw)
        assert doc.metadata["folder_id"] == "folder-1"


# ── normalize_page tests ──────────────────────────────────────────────────────

class TestNormalizePage:
    def test_stable_id_from_sha256(self) -> None:
        raw = _make_page(page_id=PAGE_ID)
        doc = normalize_page(raw, DOC_ID)
        expected = hashlib.sha256(f"page:{PAGE_ID}".encode()).hexdigest()[:16]
        assert doc.id == expected

    def test_title_extracted(self) -> None:
        raw = _make_page(name="My Canvas Page")
        doc = normalize_page(raw, DOC_ID)
        assert doc.title == "My Canvas Page"

    def test_type_is_coda_page(self) -> None:
        raw = _make_page()
        doc = normalize_page(raw, DOC_ID)
        assert doc.type == "coda_page"

    def test_browser_link_in_content(self) -> None:
        raw = _make_page()
        doc = normalize_page(raw, DOC_ID)
        assert "coda.io" in doc.content

    def test_metadata_doc_id(self) -> None:
        raw = _make_page()
        doc = normalize_page(raw, DOC_ID)
        assert doc.metadata["doc_id"] == DOC_ID
        assert doc.metadata["page_id"] == PAGE_ID

    def test_metadata_resource_type(self) -> None:
        raw = _make_page()
        doc = normalize_page(raw, DOC_ID)
        assert doc.metadata["resource_type"] == "page"
        assert doc.metadata["source"] == "coda"

    def test_parent_page_id_in_metadata(self) -> None:
        raw = _make_page()
        doc = normalize_page(raw, DOC_ID)
        assert doc.metadata["parent_page_id"] == "parent-page-1"

    def test_page_without_parent_handles_gracefully(self) -> None:
        raw = _make_page()
        raw.pop("parent", None)
        doc = normalize_page(raw, DOC_ID)
        assert doc.metadata["parent_page_id"] == ""


# ── normalize_table tests ─────────────────────────────────────────────────────

class TestNormalizeTable:
    def test_stable_id_from_sha256(self) -> None:
        raw = _make_table(table_id=TABLE_ID)
        doc = normalize_table(raw, DOC_ID)
        expected = hashlib.sha256(f"table:{TABLE_ID}".encode()).hexdigest()[:16]
        assert doc.id == expected

    def test_title_extracted(self) -> None:
        raw = _make_table(name="My Data Table")
        doc = normalize_table(raw, DOC_ID)
        assert doc.title == "My Data Table"

    def test_type_is_coda_table(self) -> None:
        raw = _make_table()
        doc = normalize_table(raw, DOC_ID)
        assert doc.type == "coda_table"

    def test_row_count_in_content(self) -> None:
        raw = _make_table(row_count=42)
        doc = normalize_table(raw, DOC_ID)
        assert "42" in doc.content

    def test_column_names_in_content(self) -> None:
        raw = _make_table()
        doc = normalize_table(raw, DOC_ID)
        assert "Name" in doc.content
        assert "Status" in doc.content

    def test_column_names_in_metadata(self) -> None:
        raw = _make_table()
        doc = normalize_table(raw, DOC_ID)
        assert "Name" in doc.metadata["columns"]
        assert "Status" in doc.metadata["columns"]

    def test_metadata_doc_id_and_table_id(self) -> None:
        raw = _make_table()
        doc = normalize_table(raw, DOC_ID)
        assert doc.metadata["doc_id"] == DOC_ID
        assert doc.metadata["table_id"] == TABLE_ID

    def test_metadata_resource_type(self) -> None:
        raw = _make_table()
        doc = normalize_table(raw, DOC_ID)
        assert doc.metadata["resource_type"] == "table"
        assert doc.metadata["source"] == "coda"


# ── normalize_row tests ───────────────────────────────────────────────────────

class TestNormalizeRow:
    def test_stable_id_from_sha256(self) -> None:
        raw = _make_row(row_id=ROW_ID)
        doc = normalize_row(raw, DOC_ID, TABLE_ID)
        expected = hashlib.sha256(f"row:{ROW_ID}".encode()).hexdigest()[:16]
        assert doc.id == expected

    def test_type_is_coda_row(self) -> None:
        raw = _make_row()
        doc = normalize_row(raw, DOC_ID, TABLE_ID)
        assert doc.type == "coda_row"

    def test_content_contains_cells_json(self) -> None:
        raw = _make_row()
        doc = normalize_row(raw, DOC_ID, TABLE_ID)
        assert "col-a" in doc.content
        assert "Alice" in doc.content

    def test_cells_are_valid_json_in_content(self) -> None:
        raw = _make_row()
        doc = normalize_row(raw, DOC_ID, TABLE_ID)
        # Extract JSON portion from content
        cells_line = [line for line in doc.content.split("\n") if line.startswith("Cells:")][0]
        json_str = cells_line[len("Cells: "):]
        parsed = json.loads(json_str)
        assert parsed["col-a"] == "Alice"

    def test_metadata_row_id_doc_id_table_id(self) -> None:
        raw = _make_row()
        doc = normalize_row(raw, DOC_ID, TABLE_ID)
        assert doc.metadata["row_id"] == ROW_ID
        assert doc.metadata["doc_id"] == DOC_ID
        assert doc.metadata["table_id"] == TABLE_ID

    def test_metadata_values_preserved(self) -> None:
        raw = _make_row()
        doc = normalize_row(raw, DOC_ID, TABLE_ID)
        assert doc.metadata["values"]["col-a"] == "Alice"
        assert doc.metadata["values"]["col-b"] == "Done"

    def test_metadata_resource_type(self) -> None:
        raw = _make_row()
        doc = normalize_row(raw, DOC_ID, TABLE_ID)
        assert doc.metadata["resource_type"] == "row"
        assert doc.metadata["source"] == "coda"

    def test_row_with_empty_values(self) -> None:
        raw = _make_row()
        raw["values"] = {}
        doc = normalize_row(raw, DOC_ID, TABLE_ID)
        assert doc.metadata["values"] == {}
        assert "{}" in doc.content

    def test_row_name_is_title(self) -> None:
        raw = _make_row(name="My Row Name")
        doc = normalize_row(raw, DOC_ID, TABLE_ID)
        assert doc.title == "My Row Name"


# ── with_retry tests ──────────────────────────────────────────────────────────

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        mock_fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock_fn, max_attempts=3)
        assert result == {"ok": True}
        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_coda_error(self) -> None:
        calls = 0

        async def flaky() -> Dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise CodaError("temporary error")
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(flaky, max_attempts=3)
        assert result == {"ok": True}
        assert calls == 3

    @pytest.mark.asyncio
    async def test_does_not_retry_on_auth_error(self) -> None:
        mock_fn = AsyncMock(side_effect=CodaAuthError("invalid token"))
        with pytest.raises(CodaAuthError):
            await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_does_not_retry_on_not_found_error(self) -> None:
        mock_fn = AsyncMock(side_effect=CodaNotFoundError("doc not found"))
        with pytest.raises(CodaNotFoundError):
            await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=CodaError("always fails"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(CodaError, match="always fails"):
                await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 3

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit_error(self) -> None:
        calls = 0

        async def rate_limited() -> Dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise CodaRateLimitError("429")
            return {"data": "ok"}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(rate_limited, max_attempts=3)
        assert result == {"data": "ok"}
        assert calls == 2

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self) -> None:
        calls = 0

        async def network_fail() -> Dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise CodaNetworkError("connection refused")
            return {"data": "recovered"}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(network_fail, max_attempts=3)
        assert result == {"data": "recovered"}
        assert calls == 2


# ── HTTP client tests ─────────────────────────────────────────────────────────

class TestCodaHTTPClient:
    def test_auth_header_uses_bearer_token(self) -> None:
        client = _make_client()
        headers = client._auth_headers()
        assert headers["Authorization"] == f"Bearer {TOKEN}"

    def test_auth_header_content_type_json(self) -> None:
        client = _make_client()
        headers = client._auth_headers()
        assert headers["Content-Type"] == "application/json"

    def test_raise_for_status_200_returns_data(self) -> None:
        client = _make_client()
        data = {"id": "doc-1", "name": "My Doc"}
        result = client._raise_for_status(200, data, "test")
        assert result == data

    def test_raise_for_status_401_raises_auth_error(self) -> None:
        client = _make_client()
        with pytest.raises(CodaAuthError):
            client._raise_for_status(401, {"message": "Unauthorized"}, "test")

    def test_raise_for_status_403_raises_auth_error(self) -> None:
        client = _make_client()
        with pytest.raises(CodaAuthError):
            client._raise_for_status(403, {"message": "Forbidden"}, "test")

    def test_raise_for_status_404_raises_not_found(self) -> None:
        client = _make_client()
        with pytest.raises(CodaNotFoundError):
            client._raise_for_status(404, {"message": "Not found"}, "test")

    def test_raise_for_status_429_raises_rate_limit(self) -> None:
        client = _make_client()
        with pytest.raises(CodaRateLimitError):
            client._raise_for_status(429, {}, "test")

    def test_raise_for_status_500_raises_network_error(self) -> None:
        client = _make_client()
        with pytest.raises(CodaNetworkError):
            client._raise_for_status(500, {"message": "Internal error"}, "test")

    def test_raise_for_status_503_raises_network_error(self) -> None:
        client = _make_client()
        with pytest.raises(CodaNetworkError):
            client._raise_for_status(503, {"message": "Service unavailable"}, "test")

    def test_raise_for_status_400_raises_coda_error(self) -> None:
        client = _make_client()
        with pytest.raises(CodaError):
            client._raise_for_status(400, {"message": "Bad request"}, "test")

    @pytest.mark.asyncio
    async def test_get_who_am_i_success(self) -> None:
        client = _make_client()
        whoami = _make_whoami()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=whoami)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_who_am_i()
        assert result["loginId"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_get_who_am_i_401_raises_auth_error(self) -> None:
        client = _make_client()

        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.json = AsyncMock(return_value={"message": "Invalid API token"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(CodaAuthError):
                await client.get_who_am_i()

    @pytest.mark.asyncio
    async def test_get_docs_success(self) -> None:
        client = _make_client()
        docs_resp = _make_list_response([_make_doc()])

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=docs_resp)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_docs()
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == DOC_ID

    @pytest.mark.asyncio
    async def test_get_docs_with_page_token(self) -> None:
        client = _make_client()
        docs_resp = _make_list_response([_make_doc(doc_id="doc-page-2")])

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=docs_resp)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_docs(page_token="tok-abc")
        assert result["items"][0]["id"] == "doc-page-2"

    @pytest.mark.asyncio
    async def test_get_doc_success(self) -> None:
        client = _make_client()
        doc = _make_doc()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=doc)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_doc(DOC_ID)
        assert result["id"] == DOC_ID
        assert result["name"] == "My Test Doc"

    @pytest.mark.asyncio
    async def test_get_doc_404_raises_not_found(self) -> None:
        client = _make_client()

        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.json = AsyncMock(return_value={"message": "Doc not found"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(CodaNotFoundError):
                await client.get_doc("nonexistent-doc")

    @pytest.mark.asyncio
    async def test_get_pages_success(self) -> None:
        client = _make_client()
        pages_resp = _make_list_response([_make_page()])

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=pages_resp)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_pages(DOC_ID)
        assert len(result["items"]) == 1

    @pytest.mark.asyncio
    async def test_get_tables_success(self) -> None:
        client = _make_client()
        tables_resp = _make_list_response([_make_table()])

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=tables_resp)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_tables(DOC_ID)
        assert len(result["items"]) == 1

    @pytest.mark.asyncio
    async def test_get_rows_success(self) -> None:
        client = _make_client()
        rows_resp = _make_list_response([_make_row()])

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=rows_resp)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_rows(DOC_ID, TABLE_ID)
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == ROW_ID

    @pytest.mark.asyncio
    async def test_get_rows_uses_next_page_token(self) -> None:
        client = _make_client()
        rows_resp = _make_list_response([_make_row(row_id="row-page-2")], next_page_token=None)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=rows_resp)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_rows(DOC_ID, TABLE_ID, page_token="tok-xyz")
        assert result["items"][0]["id"] == "row-page-2"

    @pytest.mark.asyncio
    async def test_network_error_raises_coda_network_error(self) -> None:
        import aiohttp as _aiohttp
        client = _make_client()

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=_aiohttp.ClientError("connection refused"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(CodaNetworkError):
                await client.get_who_am_i()

    @pytest.mark.asyncio
    async def test_get_docs_network_error(self) -> None:
        import aiohttp as _aiohttp
        client = _make_client()

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=_aiohttp.ClientError("timeout"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(CodaNetworkError):
                await client.get_docs()


# ── connector install tests ───────────────────────────────────────────────────

class TestCodaConnectorInstall:
    @pytest.mark.asyncio
    async def test_install_with_token_returns_healthy(self) -> None:
        connector = _make_connector()
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    @pytest.mark.asyncio
    async def test_install_without_token_returns_offline(self) -> None:
        connector = CodaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={},
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_with_empty_token_returns_offline(self) -> None:
        connector = CodaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_token": ""},
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_message_contains_api_token(self) -> None:
        connector = _make_connector()
        result = await connector.install()
        assert "api token" in result.message.lower() or "installed" in result.message.lower()

    @pytest.mark.asyncio
    async def test_install_missing_message_contains_field_name(self) -> None:
        connector = CodaConnector(config={})
        result = await connector.install()
        assert "api_token" in result.message


# ── connector health_check tests ──────────────────────────────────────────────

class TestCodaConnectorHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_success(self) -> None:
        connector = _make_connector()
        whoami = _make_whoami(name="Alice")

        with patch.object(
            connector.client,
            "get_who_am_i",
            new_callable=AsyncMock,
            return_value=whoami,
        ):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Alice" in result.message

    @pytest.mark.asyncio
    async def test_health_check_falls_back_to_login_id(self) -> None:
        connector = _make_connector()
        whoami = {"loginId": "user@example.com"}

        with patch.object(
            connector.client,
            "get_who_am_i",
            new_callable=AsyncMock,
            return_value=whoami,
        ):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.HEALTHY
        assert "user@example.com" in result.message

    @pytest.mark.asyncio
    async def test_health_check_auth_error_returns_degraded(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector.client,
            "get_who_am_i",
            new_callable=AsyncMock,
            side_effect=CodaAuthError("invalid token"),
        ):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error_returns_degraded(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector.client,
            "get_who_am_i",
            new_callable=AsyncMock,
            side_effect=CodaNetworkError("timeout"),
        ):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_unexpected_error_returns_degraded(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector.client,
            "get_who_am_i",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected"),
        ):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.DEGRADED


# ── connector list_docs tests ─────────────────────────────────────────────────

class TestCodaConnectorListDocs:
    @pytest.mark.asyncio
    async def test_list_docs_returns_items(self) -> None:
        connector = _make_connector()
        docs_resp = _make_list_response([_make_doc(), _make_doc(doc_id="doc-2", name="Doc 2")])

        with patch.object(connector.client, "get_docs", new_callable=AsyncMock, return_value=docs_resp):
            docs = await connector.list_docs()

        assert len(docs) == 2

    @pytest.mark.asyncio
    async def test_list_docs_follows_pagination(self) -> None:
        connector = _make_connector()
        call_count = 0

        async def mock_get_docs(**kwargs: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_list_response([_make_doc(doc_id="doc-a")], next_page_token="tok-1")
            return _make_list_response([_make_doc(doc_id="doc-b")])

        with patch.object(connector.client, "get_docs", side_effect=mock_get_docs):
            docs = await connector.list_docs()

        assert len(docs) == 2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_list_docs_empty(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector.client,
            "get_docs",
            new_callable=AsyncMock,
            return_value=_make_list_response([]),
        ):
            docs = await connector.list_docs()

        assert docs == []


# ── connector list_pages tests ────────────────────────────────────────────────

class TestCodaConnectorListPages:
    @pytest.mark.asyncio
    async def test_list_pages_returns_items(self) -> None:
        connector = _make_connector()
        pages_resp = _make_list_response([_make_page(), _make_page(page_id="p-2", name="Page 2")])

        with patch.object(connector.client, "get_pages", new_callable=AsyncMock, return_value=pages_resp):
            pages = await connector.list_pages(DOC_ID)

        assert len(pages) == 2

    @pytest.mark.asyncio
    async def test_list_pages_follows_pagination(self) -> None:
        connector = _make_connector()
        call_count = 0

        async def mock_get_pages(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_list_response([_make_page(page_id="p-a")], next_page_token="tok-p1")
            return _make_list_response([_make_page(page_id="p-b")])

        with patch.object(connector.client, "get_pages", side_effect=mock_get_pages):
            pages = await connector.list_pages(DOC_ID)

        assert len(pages) == 2
        assert call_count == 2


# ── connector list_tables tests ───────────────────────────────────────────────

class TestCodaConnectorListTables:
    @pytest.mark.asyncio
    async def test_list_tables_returns_items(self) -> None:
        connector = _make_connector()
        tables_resp = _make_list_response([_make_table()])

        with patch.object(connector.client, "get_tables", new_callable=AsyncMock, return_value=tables_resp):
            tables = await connector.list_tables(DOC_ID)

        assert len(tables) == 1

    @pytest.mark.asyncio
    async def test_list_tables_follows_pagination(self) -> None:
        connector = _make_connector()
        call_count = 0

        async def mock_get_tables(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_list_response([_make_table(table_id="t-a")], next_page_token="tok-t1")
            return _make_list_response([_make_table(table_id="t-b")])

        with patch.object(connector.client, "get_tables", side_effect=mock_get_tables):
            tables = await connector.list_tables(DOC_ID)

        assert len(tables) == 2
        assert call_count == 2


# ── connector list_rows tests ─────────────────────────────────────────────────

class TestCodaConnectorListRows:
    @pytest.mark.asyncio
    async def test_list_rows_returns_items(self) -> None:
        connector = _make_connector()
        rows_resp = _make_list_response([_make_row(), _make_row(row_id="r-2", name="Row 2")])

        with patch.object(connector.client, "get_rows", new_callable=AsyncMock, return_value=rows_resp):
            rows = await connector.list_rows(DOC_ID, TABLE_ID)

        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_list_rows_follows_pagination(self) -> None:
        connector = _make_connector()
        call_count = 0

        async def mock_get_rows(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_list_response([_make_row(row_id="r-a")], next_page_token="tok-r1")
            return _make_list_response([_make_row(row_id="r-b")])

        with patch.object(connector.client, "get_rows", side_effect=mock_get_rows):
            rows = await connector.list_rows(DOC_ID, TABLE_ID)

        assert len(rows) == 2
        assert call_count == 2


# ── connector get_doc tests ───────────────────────────────────────────────────

class TestCodaConnectorGetDoc:
    @pytest.mark.asyncio
    async def test_get_doc_success(self) -> None:
        connector = _make_connector()
        doc = _make_doc()

        with patch.object(connector.client, "get_doc", new_callable=AsyncMock, return_value=doc):
            result = await connector.get_doc(DOC_ID)

        assert result["id"] == DOC_ID
        assert result["name"] == "My Test Doc"

    @pytest.mark.asyncio
    async def test_get_doc_not_found_raises(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector.client,
            "get_doc",
            new_callable=AsyncMock,
            side_effect=CodaNotFoundError("Doc not found"),
        ):
            with pytest.raises(CodaNotFoundError):
                await connector.get_doc("nonexistent-doc-id")

    @pytest.mark.asyncio
    async def test_get_doc_auth_error_raises(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector.client,
            "get_doc",
            new_callable=AsyncMock,
            side_effect=CodaAuthError("invalid token"),
        ):
            with pytest.raises(CodaAuthError):
                await connector.get_doc(DOC_ID)


# ── connector sync tests ──────────────────────────────────────────────────────

class TestCodaConnectorSync:
    @pytest.mark.asyncio
    async def test_sync_single_doc_with_page_and_table(self) -> None:
        connector = _make_connector()

        with patch.object(connector, "list_docs", new_callable=AsyncMock, return_value=[_make_doc()]):
            with patch.object(connector, "list_pages", new_callable=AsyncMock, return_value=[_make_page()]):
                with patch.object(connector, "list_tables", new_callable=AsyncMock, return_value=[_make_table()]):
                    with patch.object(connector, "list_rows", new_callable=AsyncMock, return_value=[_make_row()]):
                        result = await connector.sync()

        # 1 doc + 1 page + 1 table + 1 row = 4
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert result.documents_failed == 0

    @pytest.mark.asyncio
    async def test_sync_empty_workspace(self) -> None:
        connector = _make_connector()

        with patch.object(connector, "list_docs", new_callable=AsyncMock, return_value=[]):
            result = await connector.sync()

        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    @pytest.mark.asyncio
    async def test_sync_multiple_docs(self) -> None:
        connector = _make_connector()
        docs = [_make_doc(doc_id="d-1"), _make_doc(doc_id="d-2")]

        with patch.object(connector, "list_docs", new_callable=AsyncMock, return_value=docs):
            with patch.object(connector, "list_pages", new_callable=AsyncMock, return_value=[]):
                with patch.object(connector, "list_tables", new_callable=AsyncMock, return_value=[]):
                    result = await connector.sync()

        assert result.documents_synced == 2

    @pytest.mark.asyncio
    async def test_sync_pages_per_doc(self) -> None:
        connector = _make_connector()

        with patch.object(connector, "list_docs", new_callable=AsyncMock, return_value=[_make_doc()]):
            with patch.object(
                connector, "list_pages", new_callable=AsyncMock,
                return_value=[_make_page(page_id="p-1"), _make_page(page_id="p-2")],
            ):
                with patch.object(connector, "list_tables", new_callable=AsyncMock, return_value=[]):
                    result = await connector.sync()

        # 1 doc + 2 pages
        assert result.documents_synced == 3

    @pytest.mark.asyncio
    async def test_sync_tables_per_doc(self) -> None:
        connector = _make_connector()

        with patch.object(connector, "list_docs", new_callable=AsyncMock, return_value=[_make_doc()]):
            with patch.object(connector, "list_pages", new_callable=AsyncMock, return_value=[]):
                with patch.object(
                    connector, "list_tables", new_callable=AsyncMock,
                    return_value=[_make_table(table_id="t-1"), _make_table(table_id="t-2")],
                ):
                    with patch.object(connector, "list_rows", new_callable=AsyncMock, return_value=[]):
                        result = await connector.sync()

        # 1 doc + 2 tables
        assert result.documents_synced == 3

    @pytest.mark.asyncio
    async def test_sync_rows_per_table(self) -> None:
        connector = _make_connector()

        with patch.object(connector, "list_docs", new_callable=AsyncMock, return_value=[_make_doc()]):
            with patch.object(connector, "list_pages", new_callable=AsyncMock, return_value=[]):
                with patch.object(connector, "list_tables", new_callable=AsyncMock, return_value=[_make_table()]):
                    with patch.object(
                        connector, "list_rows", new_callable=AsyncMock,
                        return_value=[_make_row(row_id="r-1"), _make_row(row_id="r-2"), _make_row(row_id="r-3")],
                    ):
                        result = await connector.sync()

        # 1 doc + 1 table + 3 rows
        assert result.documents_synced == 5

    @pytest.mark.asyncio
    async def test_sync_failed_on_list_docs_error(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector,
            "list_docs",
            new_callable=AsyncMock,
            side_effect=CodaAuthError("invalid token"),
        ):
            result = await connector.sync()

        assert result.status == SyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_sync_message_contains_counts(self) -> None:
        connector = _make_connector()

        with patch.object(connector, "list_docs", new_callable=AsyncMock, return_value=[_make_doc()]):
            with patch.object(connector, "list_pages", new_callable=AsyncMock, return_value=[]):
                with patch.object(connector, "list_tables", new_callable=AsyncMock, return_value=[]):
                    result = await connector.sync()

        assert "1" in result.message

    @pytest.mark.asyncio
    async def test_sync_partial_on_page_normalization_failure(self) -> None:
        connector = _make_connector()
        bad_page: Dict[str, Any] = {}  # missing required fields → should fail in some way

        # Patch list_docs / list_pages / list_tables / list_rows at a low level
        with patch.object(connector, "list_docs", new_callable=AsyncMock, return_value=[_make_doc()]):
            with patch.object(connector, "list_pages", new_callable=AsyncMock, return_value=[bad_page]):
                with patch.object(connector, "list_tables", new_callable=AsyncMock, return_value=[]):
                    with patch("connector.normalize_page", side_effect=Exception("normalize failed")):
                        result = await connector.sync()

        # 1 doc synced, 1 page failed
        assert result.documents_failed >= 1


# ── connector init tests ──────────────────────────────────────────────────────

class TestCodaConnectorInit:
    def test_default_init(self) -> None:
        connector = CodaConnector()
        assert connector.tenant_id == ""
        assert connector.connector_id == ""
        assert connector.config == {}

    def test_init_with_config(self) -> None:
        connector = _make_connector()
        assert connector.tenant_id == TENANT
        assert connector.connector_id == CONNECTOR_ID
        assert connector.config["api_token"] == TOKEN

    def test_connector_type_constant(self) -> None:
        assert CodaConnector.CONNECTOR_TYPE == "coda"
        from connector import CONNECTOR_TYPE
        assert CONNECTOR_TYPE == "coda"

    def test_auth_type_constant(self) -> None:
        assert CodaConnector.AUTH_TYPE == "api_key"
        from connector import AUTH_TYPE
        assert AUTH_TYPE == "api_key"

    def test_connector_name(self) -> None:
        assert CodaConnector.CONNECTOR_NAME == "Coda"

    def test_required_config_keys(self) -> None:
        assert "api_token" in CodaConnector.REQUIRED_CONFIG_KEYS

    def test_client_is_http_client_instance(self) -> None:
        connector = _make_connector()
        assert isinstance(connector.client, CodaHTTPClient)

    def test_get_token_returns_api_token(self) -> None:
        connector = _make_connector()
        assert connector._get_token() == TOKEN

    def test_get_token_returns_empty_when_missing(self) -> None:
        connector = CodaConnector(config={})
        assert connector._get_token() == ""

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        async with CodaConnector(config={"api_token": TOKEN}) as connector:
            assert isinstance(connector, CodaConnector)

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self) -> None:
        connector = _make_connector()
        await connector.aclose()
        await connector.aclose()  # should not raise
