"""Unit tests for AirtableConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AirtableConnector
from exceptions import (
    AirtableAuthError,
    AirtableError,
    AirtableNetworkError,
    AirtableNotFoundError,
    AirtableRateLimitError,
)
from helpers.utils import normalize_record, normalize_table, with_retry, _stable_id
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ──────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_airtable_test_001"
API_KEY = "patABCDEFGHIJKLMNOP.xxxxxxxxxxxx"

SAMPLE_WHOAMI: dict = {
    "id": "usrXXXXXXXXXXXXXX",
    "email": "user@example.com",
    "scopes": ["data.records:read", "schema.bases:read"],
}

SAMPLE_BASE: dict = {
    "id": "appBASE0000000001",
    "name": "My Projects",
    "permissionLevel": "create",
}

SAMPLE_BASES_PAGE: dict = {
    "bases": [SAMPLE_BASE],
    "offset": None,
}

SAMPLE_TABLE: dict = {
    "id": "tblTABLE000000001",
    "name": "Tasks",
    "primaryFieldId": "fldFIELD00000001",
    "fields": [
        {"id": "fldFIELD00000001", "name": "Name", "type": "singleLineText"},
        {"id": "fldFIELD00000002", "name": "Status", "type": "singleSelect"},
    ],
    "views": [
        {"id": "viwVIEW000000001", "name": "Grid view", "type": "grid"},
    ],
}

SAMPLE_TABLES_RESPONSE: dict = {
    "tables": [SAMPLE_TABLE],
}

SAMPLE_RECORD: dict = {
    "id": "recRECORD00000001",
    "createdTime": "2026-06-01T10:00:00.000Z",
    "fields": {
        "Name": "Build Airtable connector",
        "Status": "In progress",
    },
}

SAMPLE_RECORD_2: dict = {
    "id": "recRECORD00000002",
    "createdTime": "2026-06-02T09:00:00.000Z",
    "fields": {
        "Name": "Write unit tests",
        "Status": "Done",
    },
}

SAMPLE_RECORDS_PAGE: dict = {
    "records": [SAMPLE_RECORD],
    "offset": None,
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> AirtableConnector:
    return AirtableConnector(
        api_key=API_KEY,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: AirtableConnector) -> AirtableConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


# ── install() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success(connector: AirtableConnector) -> None:
    with patch("connector.AirtableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.whoami = AsyncMock(return_value=SAMPLE_WHOAMI)
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "user@example.com" in result.message


@pytest.mark.asyncio
async def test_install_success_uses_id_when_no_email(connector: AirtableConnector) -> None:
    whoami_no_email = {"id": "usrXXX", "scopes": []}
    with patch("connector.AirtableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.whoami = AsyncMock(return_value=whoami_no_email)
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert "usrXXX" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = AirtableConnector(tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_invalid_token(connector: AirtableConnector) -> None:
    with patch("connector.AirtableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.whoami = AsyncMock(
            side_effect=AirtableAuthError("Invalid token", 401)
        )
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid token" in result.message


@pytest.mark.asyncio
async def test_install_network_error(connector: AirtableConnector) -> None:
    with patch("connector.AirtableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.whoami = AsyncMock(
            side_effect=AirtableNetworkError("Connection refused")
        )
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_error(connector: AirtableConnector) -> None:
    with patch("connector.AirtableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.whoami = AsyncMock(side_effect=RuntimeError("unexpected"))
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: AirtableConnector) -> None:
    with patch("connector.AirtableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.whoami = AsyncMock(return_value=SAMPLE_WHOAMI)
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "user@example.com" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: AirtableConnector) -> None:
    with patch("connector.AirtableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.whoami = AsyncMock(
            side_effect=AirtableAuthError("Forbidden", 403)
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: AirtableConnector) -> None:
    with patch("connector.AirtableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.whoami = AsyncMock(
            side_effect=AirtableNetworkError("Timeout")
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: AirtableConnector) -> None:
    with patch("connector.AirtableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.whoami = AsyncMock(side_effect=AirtableError("server error", 500))
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = AirtableConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty_bases(connector_with_mock_client: AirtableConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_bases = AsyncMock(return_value={"bases": [], "offset": None})
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_single_record(connector_with_mock_client: AirtableConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_bases = AsyncMock(return_value=SAMPLE_BASES_PAGE)
    c._http_client.list_tables = AsyncMock(return_value=SAMPLE_TABLES_RESPONSE)
    c._http_client.list_records = AsyncMock(return_value=SAMPLE_RECORDS_PAGE)
    result = await c.sync(kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_record_pagination(connector_with_mock_client: AirtableConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_bases = AsyncMock(return_value=SAMPLE_BASES_PAGE)
    c._http_client.list_tables = AsyncMock(return_value=SAMPLE_TABLES_RESPONSE)
    page1 = {"records": [SAMPLE_RECORD], "offset": "offset_cursor_abc"}
    page2 = {"records": [SAMPLE_RECORD_2], "offset": None}
    c._http_client.list_records = AsyncMock(side_effect=[page1, page2])
    result = await c.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert c._http_client.list_records.call_count == 2


@pytest.mark.asyncio
async def test_sync_multiple_tables(connector_with_mock_client: AirtableConnector) -> None:
    c = connector_with_mock_client
    table2 = {**SAMPLE_TABLE, "id": "tblTABLE000000002", "name": "Notes"}
    c._http_client.list_bases = AsyncMock(return_value=SAMPLE_BASES_PAGE)
    c._http_client.list_tables = AsyncMock(
        return_value={"tables": [SAMPLE_TABLE, table2]}
    )
    c._http_client.list_records = AsyncMock(return_value=SAMPLE_RECORDS_PAGE)
    result = await c.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert c._http_client.list_records.call_count == 2


@pytest.mark.asyncio
async def test_sync_multiple_bases(connector_with_mock_client: AirtableConnector) -> None:
    c = connector_with_mock_client
    base2 = {"id": "appBASE0000000002", "name": "HR Database"}
    c._http_client.list_bases = AsyncMock(
        return_value={"bases": [SAMPLE_BASE, base2], "offset": None}
    )
    c._http_client.list_tables = AsyncMock(return_value=SAMPLE_TABLES_RESPONSE)
    c._http_client.list_records = AsyncMock(return_value=SAMPLE_RECORDS_PAGE)
    result = await c.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_base_pagination(connector_with_mock_client: AirtableConnector) -> None:
    c = connector_with_mock_client
    base2 = {"id": "appBASE0000000002", "name": "Second Base"}
    page1 = {"bases": [SAMPLE_BASE], "offset": "base_cursor"}
    page2 = {"bases": [base2], "offset": None}
    c._http_client.list_bases = AsyncMock(side_effect=[page1, page2])
    c._http_client.list_tables = AsyncMock(return_value=SAMPLE_TABLES_RESPONSE)
    c._http_client.list_records = AsyncMock(return_value=SAMPLE_RECORDS_PAGE)
    result = await c.sync()
    assert result.documents_found == 2
    assert c._http_client.list_bases.call_count == 2


@pytest.mark.asyncio
async def test_sync_list_bases_error_returns_failed(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_bases = AsyncMock(
        side_effect=AirtableError("server error", 500)
    )
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_list_tables_error_counts_failed(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_bases = AsyncMock(return_value=SAMPLE_BASES_PAGE)
    c._http_client.list_tables = AsyncMock(
        side_effect=AirtableError("tables error", 500)
    )
    result = await c.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1


@pytest.mark.asyncio
async def test_sync_list_records_error_counts_failed(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_bases = AsyncMock(return_value=SAMPLE_BASES_PAGE)
    c._http_client.list_tables = AsyncMock(return_value=SAMPLE_TABLES_RESPONSE)
    c._http_client.list_records = AsyncMock(
        side_effect=AirtableError("records error", 500)
    )
    result = await c.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1


@pytest.mark.asyncio
async def test_sync_skips_table_with_empty_name(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    empty_name_table = {**SAMPLE_TABLE, "name": ""}
    c._http_client.list_bases = AsyncMock(return_value=SAMPLE_BASES_PAGE)
    c._http_client.list_tables = AsyncMock(
        return_value={"tables": [empty_name_table]}
    )
    c._http_client.list_records = AsyncMock(return_value=SAMPLE_RECORDS_PAGE)
    result = await c.sync()
    c._http_client.list_records.assert_not_called()
    assert result.documents_found == 0


# ── list_bases() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_bases_returns_bases(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_bases = AsyncMock(return_value=SAMPLE_BASES_PAGE)
    result = await c.list_bases()
    assert "bases" in result
    assert len(result["bases"]) == 1
    assert result["bases"][0]["id"] == "appBASE0000000001"


@pytest.mark.asyncio
async def test_list_bases_passes_offset(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_bases = AsyncMock(return_value=SAMPLE_BASES_PAGE)
    await c.list_bases(offset="some_offset")
    call_args = c._http_client.list_bases.call_args
    assert "some_offset" in call_args.args


@pytest.mark.asyncio
async def test_list_bases_no_offset_default(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_bases = AsyncMock(return_value=SAMPLE_BASES_PAGE)
    await c.list_bases()
    call_args = c._http_client.list_bases.call_args
    assert None in call_args.args


# ── list_tables() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tables_returns_tables(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_tables = AsyncMock(return_value=SAMPLE_TABLES_RESPONSE)
    result = await c.list_tables("appBASE0000000001")
    assert "tables" in result
    assert result["tables"][0]["name"] == "Tasks"


@pytest.mark.asyncio
async def test_list_tables_passes_base_id(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_tables = AsyncMock(return_value=SAMPLE_TABLES_RESPONSE)
    await c.list_tables("appBASE0000000001")
    call_args = c._http_client.list_tables.call_args
    assert "appBASE0000000001" in call_args.args


# ── list_records() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_records_returns_records(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_records = AsyncMock(return_value=SAMPLE_RECORDS_PAGE)
    result = await c.list_records("appBASE0000000001", "Tasks")
    assert "records" in result
    assert result["records"][0]["id"] == "recRECORD00000001"


@pytest.mark.asyncio
async def test_list_records_default_page_size(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_records = AsyncMock(return_value=SAMPLE_RECORDS_PAGE)
    await c.list_records("appBASE0000000001", "Tasks")
    call_args = c._http_client.list_records.call_args
    assert 100 in call_args.args


@pytest.mark.asyncio
async def test_list_records_custom_page_size(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_records = AsyncMock(return_value=SAMPLE_RECORDS_PAGE)
    await c.list_records("appBASE0000000001", "Tasks", page_size=25)
    call_args = c._http_client.list_records.call_args
    assert 25 in call_args.args


@pytest.mark.asyncio
async def test_list_records_passes_offset(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_records = AsyncMock(return_value=SAMPLE_RECORDS_PAGE)
    await c.list_records("appBASE0000000001", "Tasks", offset="cursor123")
    call_args = c._http_client.list_records.call_args
    assert "cursor123" in call_args.args


# ── get_record() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_record_returns_record(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_record = AsyncMock(return_value=SAMPLE_RECORD)
    result = await c.get_record("appBASE0000000001", "Tasks", "recRECORD00000001")
    assert result["id"] == "recRECORD00000001"
    assert result["fields"]["Name"] == "Build Airtable connector"


@pytest.mark.asyncio
async def test_get_record_passes_all_args(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_record = AsyncMock(return_value=SAMPLE_RECORD)
    await c.get_record("appBASE0000000001", "Tasks", "recRECORD00000001")
    call_args = c._http_client.get_record.call_args
    assert "appBASE0000000001" in call_args.args
    assert "Tasks" in call_args.args
    assert "recRECORD00000001" in call_args.args


@pytest.mark.asyncio
async def test_get_record_not_found_raises(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_record = AsyncMock(
        side_effect=AirtableNotFoundError("record", "recMISSING")
    )
    with pytest.raises(AirtableNotFoundError):
        await c.get_record("appBASE0000000001", "Tasks", "recMISSING")


# ── list_views() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_views_returns_views(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    views_response = {"views": [{"id": "viwVIEW000000001", "name": "Grid view", "type": "grid"}]}
    c._http_client.list_views = AsyncMock(return_value=views_response)
    result = await c.list_views("appBASE0000000001", "tblTABLE000000001")
    assert "views" in result
    assert result["views"][0]["name"] == "Grid view"


@pytest.mark.asyncio
async def test_list_views_passes_base_and_table_id(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_views = AsyncMock(return_value={"views": []})
    await c.list_views("appBASE0000000001", "tblTABLE000000001")
    call_args = c._http_client.list_views.call_args
    assert "appBASE0000000001" in call_args.args
    assert "tblTABLE000000001" in call_args.args


@pytest.mark.asyncio
async def test_list_views_empty_when_no_views(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_views = AsyncMock(return_value={"views": []})
    result = await c.list_views("appBASE0000000001", "tblTABLE000000001")
    assert result["views"] == []


# ── normalize_record() ────────────────────────────────────────────────────────


def test_normalize_record_basic() -> None:
    doc = normalize_record(
        SAMPLE_RECORD,
        "appBASE0000000001",
        "My Projects",
        "Tasks",
        CONNECTOR_ID,
        TENANT_ID,
    )
    assert "My Projects" in doc.title
    assert "Tasks" in doc.title
    assert "Build Airtable connector" in doc.title
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_record_source_id_is_16_chars() -> None:
    doc = normalize_record(
        SAMPLE_RECORD, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    assert len(doc.source_id) == 16


def test_normalize_record_source_id_is_hex() -> None:
    doc = normalize_record(
        SAMPLE_RECORD, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    int(doc.source_id, 16)  # raises ValueError if not hex


def test_normalize_record_source_id_is_deterministic() -> None:
    doc1 = normalize_record(
        SAMPLE_RECORD, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    doc2 = normalize_record(
        SAMPLE_RECORD, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    assert doc1.source_id == doc2.source_id


def test_normalize_record_different_ids_produce_different_source_ids() -> None:
    doc1 = normalize_record(
        SAMPLE_RECORD, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    doc2 = normalize_record(
        SAMPLE_RECORD_2, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    assert doc1.source_id != doc2.source_id


def test_normalize_record_metadata_fields() -> None:
    doc = normalize_record(
        SAMPLE_RECORD, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    meta = doc.metadata
    assert meta["record_id"] == "recRECORD00000001"
    assert meta["base_id"] == "appBASE0000000001"
    assert meta["base_name"] == "My Projects"
    assert meta["table_name"] == "Tasks"
    assert meta["created_time"] == "2026-06-01T10:00:00.000Z"
    assert meta["fields"]["Name"] == "Build Airtable connector"


def test_normalize_record_content_has_base_table() -> None:
    doc = normalize_record(
        SAMPLE_RECORD, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    assert "Base: My Projects" in doc.content
    assert "Table: Tasks" in doc.content


def test_normalize_record_content_has_all_fields() -> None:
    doc = normalize_record(
        SAMPLE_RECORD, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    assert "Build Airtable connector" in doc.content
    assert "In progress" in doc.content


def test_normalize_record_source_url_format() -> None:
    doc = normalize_record(
        SAMPLE_RECORD, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    assert doc.source_url == (
        "https://airtable.com/appBASE0000000001/Tasks/recRECORD00000001"
    )


def test_normalize_record_no_string_fields_uses_record_id_title() -> None:
    record = {"id": "recXXX", "createdTime": "2026-01-01T00:00:00Z", "fields": {}}
    doc = normalize_record(
        record, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    assert "recXXX" in doc.title


def test_normalize_record_list_field_serialized_as_json() -> None:
    record = {
        **SAMPLE_RECORD,
        "fields": {"Tags": ["python", "airtable"], "Name": "Test"},
    }
    doc = normalize_record(
        record, "appBASE0000000001", "My Projects", "Tasks", CONNECTOR_ID, TENANT_ID
    )
    assert "python" in doc.content


# ── normalize_table() ─────────────────────────────────────────────────────────


def test_normalize_table_basic() -> None:
    doc = normalize_table(SAMPLE_TABLE, "appBASE0000000001")
    assert "Tasks" in doc.title
    assert doc.source_id is not None
    assert len(doc.source_id) == 16


def test_normalize_table_source_id_is_hex() -> None:
    doc = normalize_table(SAMPLE_TABLE, "appBASE0000000001")
    int(doc.source_id, 16)


def test_normalize_table_source_id_is_deterministic() -> None:
    doc1 = normalize_table(SAMPLE_TABLE, "appBASE0000000001")
    doc2 = normalize_table(SAMPLE_TABLE, "appBASE0000000001")
    assert doc1.source_id == doc2.source_id


def test_normalize_table_different_table_ids_give_different_source_ids() -> None:
    table2 = {**SAMPLE_TABLE, "id": "tblTABLE000000002"}
    doc1 = normalize_table(SAMPLE_TABLE, "appBASE0000000001")
    doc2 = normalize_table(table2, "appBASE0000000001")
    assert doc1.source_id != doc2.source_id


def test_normalize_table_content_has_table_name() -> None:
    doc = normalize_table(SAMPLE_TABLE, "appBASE0000000001")
    assert "Tasks" in doc.content
    assert "appBASE0000000001" in doc.content


def test_normalize_table_content_has_fields() -> None:
    doc = normalize_table(SAMPLE_TABLE, "appBASE0000000001")
    assert "Name" in doc.content
    assert "Status" in doc.content


def test_normalize_table_content_has_views() -> None:
    doc = normalize_table(SAMPLE_TABLE, "appBASE0000000001")
    assert "Grid view" in doc.content


def test_normalize_table_metadata() -> None:
    doc = normalize_table(SAMPLE_TABLE, "appBASE0000000001")
    assert doc.metadata["table_id"] == "tblTABLE000000001"
    assert doc.metadata["table_name"] == "Tasks"
    assert doc.metadata["base_id"] == "appBASE0000000001"
    assert doc.metadata["primary_field_id"] == "fldFIELD00000001"
    assert len(doc.metadata["fields"]) == 2
    assert len(doc.metadata["views"]) == 1


# ── _stable_id() ─────────────────────────────────────────────────────────────


def test_stable_id_hash_format() -> None:
    sid = _stable_id("record", "recRECORD00000001")
    assert len(sid) == 16
    int(sid, 16)  # must be valid hex


def test_stable_id_deterministic() -> None:
    sid1 = _stable_id("record", "recA")
    sid2 = _stable_id("record", "recA")
    assert sid1 == sid2


def test_stable_id_different_prefixes_differ() -> None:
    sid1 = _stable_id("record", "abc")
    sid2 = _stable_id("table", "abc")
    assert sid1 != sid2


def test_stable_id_different_parts_differ() -> None:
    sid1 = _stable_id("record", "recA")
    sid2 = _stable_id("record", "recB")
    assert sid1 != sid2


# ── with_retry() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    mock_fn = AsyncMock(
        side_effect=[AirtableNetworkError("fail"), AirtableNetworkError("fail"), {"ok": True}]
    )
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=AirtableAuthError("invalid token", 401))
    with pytest.raises(AirtableAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=AirtableNetworkError("persistent failure"))
    with pytest.raises(AirtableNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(
        side_effect=AirtableRateLimitError("429", retry_after=0)
    )
    with pytest.raises(AirtableRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_hierarchy_auth_is_airtable_error() -> None:
    exc = AirtableAuthError("bad token", 401)
    assert isinstance(exc, AirtableError)


def test_exception_hierarchy_rate_limit_is_airtable_error() -> None:
    exc = AirtableRateLimitError("too fast")
    assert isinstance(exc, AirtableError)
    assert exc.retry_after == 0.0


def test_exception_hierarchy_not_found_is_airtable_error() -> None:
    exc = AirtableNotFoundError("record", "recXXX")
    assert isinstance(exc, AirtableError)
    assert exc.status_code == 404
    assert "recXXX" in str(exc)


def test_exception_hierarchy_network_is_airtable_error() -> None:
    exc = AirtableNetworkError("timeout", 500)
    assert isinstance(exc, AirtableError)


def test_rate_limit_stores_retry_after() -> None:
    exc = AirtableRateLimitError("slow down", retry_after=30.0)
    assert exc.retry_after == 30.0


def test_airtable_error_stores_status_and_code() -> None:
    exc = AirtableError("generic", status_code=400, code="bad_request")
    assert exc.status_code == 400
    assert exc.code == "bad_request"


# ── HTTP client bearer header ─────────────────────────────────────────────────


def test_http_client_bearer_header_format() -> None:
    from client.http_client import AirtableHTTPClient
    client = AirtableHTTPClient()
    headers = client._make_headers("patTOKEN123")
    assert headers["Authorization"] == "Bearer patTOKEN123"
    assert headers["Content-Type"] == "application/json"


def test_http_client_bearer_header_with_long_token() -> None:
    from client.http_client import AirtableHTTPClient
    token = "pat" + "X" * 40
    client = AirtableHTTPClient()
    headers = client._make_headers(token)
    assert headers["Authorization"] == f"Bearer {token}"


def test_http_client_meta_base_url() -> None:
    from client.http_client import AIRTABLE_META_BASE_URL
    assert AIRTABLE_META_BASE_URL == "https://api.airtable.com/v0/meta"


def test_http_client_records_base_url() -> None:
    from client.http_client import AIRTABLE_RECORDS_BASE_URL
    assert AIRTABLE_RECORDS_BASE_URL == "https://api.airtable.com/v0"


# ── _raise_for_status (422) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_response_422_raises_airtable_error() -> None:
    """422 invalid formula/request must raise AirtableError (not NotFound or Auth)."""
    from client.http_client import AirtableHTTPClient
    from unittest.mock import AsyncMock as AM, MagicMock, patch

    client = AirtableHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 422
    mock_response.headers = {}
    mock_response.json = AM(return_value={"error": {"message": "Invalid formula"}})

    with pytest.raises(AirtableError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_request"
    assert "422" in str(exc_info.value) or "Invalid formula" in str(exc_info.value)


@pytest.mark.asyncio
async def test_handle_response_401_raises_auth_error() -> None:
    from client.http_client import AirtableHTTPClient
    from unittest.mock import AsyncMock as AM, MagicMock

    client = AirtableHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.json = AM(return_value={"error": "AUTHENTICATION_REQUIRED"})

    with pytest.raises(AirtableAuthError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_handle_response_403_raises_auth_error() -> None:
    from client.http_client import AirtableHTTPClient
    from unittest.mock import AsyncMock as AM, MagicMock

    client = AirtableHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.json = AM(return_value={"error": "FORBIDDEN"})

    with pytest.raises(AirtableAuthError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_handle_response_404_raises_not_found() -> None:
    from client.http_client import AirtableHTTPClient
    from unittest.mock import AsyncMock as AM, MagicMock

    client = AirtableHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.json = AM(return_value={})

    with pytest.raises(AirtableNotFoundError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_handle_response_429_raises_rate_limit() -> None:
    from client.http_client import AirtableHTTPClient
    from unittest.mock import AsyncMock as AM, MagicMock

    client = AirtableHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "5"}
    mock_response.json = AM(return_value={})

    with pytest.raises(AirtableRateLimitError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.retry_after == 5.0


@pytest.mark.asyncio
async def test_handle_response_500_raises_network_error() -> None:
    from client.http_client import AirtableHTTPClient
    from unittest.mock import AsyncMock as AM, MagicMock

    client = AirtableHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.json = AM(return_value={})

    with pytest.raises(AirtableNetworkError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_handle_response_other_4xx_raises_airtable_error() -> None:
    from client.http_client import AirtableHTTPClient
    from unittest.mock import AsyncMock as AM, MagicMock

    client = AirtableHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 400
    mock_response.headers = {}
    mock_response.json = AM(return_value={})

    with pytest.raises(AirtableError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.status_code == 400


# ── Connector config loading ──────────────────────────────────────────────────


def test_connector_loads_api_key_from_config_dict() -> None:
    c = AirtableConnector(config={"api_key": "patFROMCONFIG"})
    assert c._api_key == "patFROMCONFIG"


def test_connector_loads_access_token_legacy_key() -> None:
    c = AirtableConnector(config={"access_token": "patLEGACY"})
    assert c._api_key == "patLEGACY"


def test_connector_kwarg_fallback_when_config_empty() -> None:
    c = AirtableConnector(api_key="patFROMKWARG")
    assert c._api_key == "patFROMKWARG"


def test_connector_config_takes_precedence_over_kwargs() -> None:
    c = AirtableConnector(
        config={"api_key": "patFROMCONFIG"},
        api_key="patFROMKWARG",
    )
    assert c._api_key == "patFROMCONFIG"


def test_connector_loads_base_id_from_config() -> None:
    c = AirtableConnector(config={"api_key": "patXXX", "base_id": "appBASE0000000001"})
    assert c._base_id == "appBASE0000000001"


def test_connector_base_id_empty_when_not_set() -> None:
    c = AirtableConnector(config={"api_key": "patXXX"})
    assert c._base_id == ""


def test_connector_missing_credentials_returns_api_key() -> None:
    c = AirtableConnector()
    missing = c._missing_credentials()
    assert "api_key" in missing


def test_connector_no_missing_credentials_when_key_present() -> None:
    c = AirtableConnector(api_key=API_KEY)
    assert c._missing_credentials() == []


def test_connector_type_constant() -> None:
    from connector import CONNECTOR_TYPE, AUTH_TYPE
    assert CONNECTOR_TYPE == "airtable"
    assert AUTH_TYPE == "api_key"


# ── Connector lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_context_manager(connector: AirtableConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(
    connector_with_mock_client: AirtableConnector,
) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


def test_ensure_client_creates_on_first_call(connector: AirtableConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: AirtableConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2


def test_connector_stores_tenant_and_connector_id() -> None:
    c = AirtableConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        api_key=API_KEY,
    )
    assert c._tenant_id == TENANT_ID
    assert c.connector_id == CONNECTOR_ID
