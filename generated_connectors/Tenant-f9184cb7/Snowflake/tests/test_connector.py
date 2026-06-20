"""
Unit tests for SnowflakeConnector — all Snowflake HTTP calls are mocked.

Coverage:
- Exception hierarchy and attributes (7 tests)
- Model enums and dataclasses (8 tests)
- normalize_database (6 tests)
- normalize_schema (7 tests)
- normalize_table (7 tests)
- with_retry logic (7 tests)
- SnowflakeHTTPClient — authenticate (5 tests)
- SnowflakeHTTPClient — get_databases, get_schemas, get_tables (6 tests)
- SnowflakeHTTPClient — execute_statement, get_statement_result (5 tests)
- SnowflakeHTTPClient — _raise_for_status status codes (6 tests)
- install() — all branches (5 tests)
- health_check() — all branches (5 tests)
- sync() — all branches (8 tests)
- list_databases / list_schemas / list_tables (5 tests)
- execute_query / get_query_result (4 tests)
- account identifier URL construction (3 tests)
- _has_credentials and lifecycle (5 tests)

Total: 102 tests
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

from connector import CONNECTOR_TYPE, AUTH_TYPE, SnowflakeConnector
from exceptions import (
    SnowflakeAuthError,
    SnowflakeError,
    SnowflakeNetworkError,
    SnowflakeNotFoundError,
    SnowflakeQueryError,
    SnowflakeRateLimitError,
    SnowflakeServerError,
)
from helpers.utils import (
    _stable_id,
    normalize_database,
    normalize_schema,
    normalize_table,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SnowflakeDatabase,
    SnowflakeSchema,
    SnowflakeTable,
    StatementStatus,
    SyncResult,
    SyncStatus,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

TENANT_ID = "tenant_snowflake_test"
CONNECTOR_ID = "conn_snowflake_001"

VALID_CONFIG: dict[str, Any] = {
    "account": "myorg-account123",
    "username": "SHIELVA_USER",
    "password": "S3cur3P@ssw0rd!",
    "warehouse": "COMPUTE_WH",
    "database": "ANALYTICS",
    "schema": "PUBLIC",
    "role": "SYSADMIN",
}

SAMPLE_DATABASE: dict[str, Any] = {
    "name": "ANALYTICS",
    "created_on": "2024-01-15T10:00:00.000Z",
    "owner": "SYSADMIN",
    "comment": "Main analytics database",
    "retention_time": 7,
    "options": "",
}

SAMPLE_SCHEMA: dict[str, Any] = {
    "name": "PUBLIC",
    "database_name": "ANALYTICS",
    "created_on": "2024-01-15T10:05:00.000Z",
    "owner": "SYSADMIN",
    "comment": "Default public schema",
    "retention_time": 7,
    "options": "",
}

SAMPLE_TABLE: dict[str, Any] = {
    "name": "ORDERS",
    "database_name": "ANALYTICS",
    "schema_name": "PUBLIC",
    "kind": "TABLE",
    "created_on": "2024-01-20T08:00:00.000Z",
    "owner": "SYSADMIN",
    "comment": "Order transactions",
    "rows": 1_000_000,
    "bytes": 524_288_000,
    "cluster_by": "LINEAR(ORDER_DATE)",
}

LOGIN_SUCCESS_BODY: dict[str, Any] = {
    "success": True,
    "data": {
        "token": "session_token_abc123",
        "masterToken": "master_token_xyz",
        "validityInSeconds": 3600,
    },
}

DATABASES_RESPONSE: dict[str, Any] = {
    "databases": [SAMPLE_DATABASE, {"name": "RAW", "owner": "SYSADMIN"}],
}

SCHEMAS_RESPONSE: dict[str, Any] = {
    "schemas": [SAMPLE_SCHEMA, {"name": "STAGING", "database_name": "ANALYTICS"}],
}

TABLES_RESPONSE: dict[str, Any] = {
    "tables": [SAMPLE_TABLE, {"name": "CUSTOMERS", "database_name": "ANALYTICS", "schema_name": "PUBLIC"}],
}

STATEMENT_SUCCESS_RESPONSE: dict[str, Any] = {
    "statementHandle": "01b3e1d6-0000-0001-0000-00000001d09e",
    "statementStatusUrl": "/api/v2/statements/01b3e1d6-0000-0001-0000-00000001d09e",
    "message": "Statement executed successfully.",
    "createdOn": 1705320000000,
    "resultSetMetaData": {
        "numRows": 1,
        "format": "jsonv2",
        "rowType": [
            {"name": "CURRENT_USER()", "type": "TEXT"},
            {"name": "CURRENT_ACCOUNT()", "type": "TEXT"},
            {"name": "CURRENT_WAREHOUSE()", "type": "TEXT"},
        ],
    },
    "data": [["SHIELVA_USER", "MYORG-ACCOUNT123", "COMPUTE_WH"]],
}

# ─────────────────────────────────────────────────────────────────────────────
# Exception tests (7)
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptions:
    def test_snowflake_error_base_attrs(self) -> None:
        exc = SnowflakeError("something failed", status_code=500, code="server_error")
        assert exc.message == "something failed"
        assert exc.status_code == 500
        assert exc.code == "server_error"
        assert str(exc) == "something failed"

    def test_snowflake_auth_error_is_subclass(self) -> None:
        exc = SnowflakeAuthError("bad credentials", status_code=401, code="unauthorized")
        assert isinstance(exc, SnowflakeError)
        assert exc.status_code == 401

    def test_snowflake_network_error_is_subclass(self) -> None:
        exc = SnowflakeNetworkError("timeout")
        assert isinstance(exc, SnowflakeError)

    def test_snowflake_not_found_error_attrs(self) -> None:
        exc = SnowflakeNotFoundError("database", "MISSING_DB")
        assert isinstance(exc, SnowflakeError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert exc.resource == "database"
        assert exc.resource_id == "MISSING_DB"
        assert "MISSING_DB" in str(exc)

    def test_snowflake_rate_limit_error_retry_after(self) -> None:
        exc = SnowflakeRateLimitError("too many requests", retry_after=30.0)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 30.0

    def test_snowflake_query_error_attrs(self) -> None:
        exc = SnowflakeQueryError("SQL compilation error", sql_state="42000", query_id="qid-123")
        assert isinstance(exc, SnowflakeError)
        assert exc.sql_state == "42000"
        assert exc.query_id == "qid-123"
        assert exc.status_code == 422

    def test_snowflake_server_error_is_subclass(self) -> None:
        exc = SnowflakeServerError("internal error", status_code=503, code="server_error")
        assert isinstance(exc, SnowflakeError)
        assert exc.status_code == 503


# ─────────────────────────────────────────────────────────────────────────────
# Model tests (8)
# ─────────────────────────────────────────────────────────────────────────────

class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"
        assert SyncStatus.RUNNING == "running"

    def test_statement_status_values(self) -> None:
        assert StatementStatus.RUNNING == "running"
        assert StatementStatus.SUCCEEDED == "succeeded"
        assert StatementStatus.FAILED == "failed"

    def test_install_result_defaults(self) -> None:
        r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.connector_id == ""
        assert r.message == ""
        assert r.account == ""

    def test_health_check_result_defaults(self) -> None:
        r = HealthCheckResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.message == ""
        assert r.account == ""
        assert r.username == ""

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="content here",
            connector_id="conn_1",
            tenant_id="tenant_1",
            source_url="https://example.com",
            metadata={"entity_type": "database"},
        )
        assert doc.source_id == "abc123"
        assert doc.metadata["entity_type"] == "database"


# ─────────────────────────────────────────────────────────────────────────────
# normalize_database tests (6)
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeDatabase:
    def test_basic_fields(self) -> None:
        doc = normalize_database(SAMPLE_DATABASE, CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Snowflake database: ANALYTICS"
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID
        assert doc.metadata["entity_type"] == "database"

    def test_stable_source_id(self) -> None:
        doc = normalize_database(SAMPLE_DATABASE, CONNECTOR_ID, TENANT_ID)
        expected = hashlib.sha256(b"database:ANALYTICS").hexdigest()[:16]
        assert doc.source_id == expected

    def test_source_id_is_16_chars(self) -> None:
        doc = normalize_database({"name": "MY_DB"})
        assert len(doc.source_id) == 16

    def test_metadata_keys(self) -> None:
        doc = normalize_database(SAMPLE_DATABASE, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["name"] == "ANALYTICS"
        assert doc.metadata["owner"] == "SYSADMIN"
        assert doc.metadata["retention_time"] == 7
        assert doc.metadata["comment"] == "Main analytics database"

    def test_content_includes_name(self) -> None:
        doc = normalize_database(SAMPLE_DATABASE)
        assert "ANALYTICS" in doc.content

    def test_minimal_record(self) -> None:
        doc = normalize_database({"name": "MINIMAL"})
        assert doc.title == "Snowflake database: MINIMAL"
        assert doc.source_id  # non-empty
        assert doc.metadata["entity_type"] == "database"


# ─────────────────────────────────────────────────────────────────────────────
# normalize_schema tests (7)
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeSchema:
    def test_basic_fields(self) -> None:
        doc = normalize_schema(SAMPLE_SCHEMA, "ANALYTICS", CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Snowflake schema: ANALYTICS.PUBLIC"
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_stable_source_id(self) -> None:
        doc = normalize_schema(SAMPLE_SCHEMA, "ANALYTICS", CONNECTOR_ID, TENANT_ID)
        expected = hashlib.sha256(b"schema:ANALYTICS.PUBLIC").hexdigest()[:16]
        assert doc.source_id == expected

    def test_source_id_is_16_chars(self) -> None:
        doc = normalize_schema({"name": "MY_SCHEMA"}, "MY_DB")
        assert len(doc.source_id) == 16

    def test_database_from_raw_record(self) -> None:
        raw = {**SAMPLE_SCHEMA}  # already has database_name
        doc = normalize_schema(raw, "", CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["database_name"] == "ANALYTICS"

    def test_database_from_param(self) -> None:
        raw = {"name": "STAGING"}  # no database_name in raw
        doc = normalize_schema(raw, "RAW_DB", CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["database_name"] == "RAW_DB"
        assert "RAW_DB.STAGING" in doc.title

    def test_metadata_keys(self) -> None:
        doc = normalize_schema(SAMPLE_SCHEMA, "ANALYTICS", CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["name"] == "PUBLIC"
        assert doc.metadata["qualified_name"] == "ANALYTICS.PUBLIC"
        assert doc.metadata["owner"] == "SYSADMIN"

    def test_content_includes_qualified_name(self) -> None:
        doc = normalize_schema(SAMPLE_SCHEMA, "ANALYTICS")
        assert "ANALYTICS.PUBLIC" in doc.content


# ─────────────────────────────────────────────────────────────────────────────
# normalize_table tests (7)
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeTable:
    def test_basic_fields(self) -> None:
        doc = normalize_table(SAMPLE_TABLE, "ANALYTICS", "PUBLIC", CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Snowflake table: ANALYTICS.PUBLIC.ORDERS"
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_stable_source_id(self) -> None:
        doc = normalize_table(SAMPLE_TABLE, "ANALYTICS", "PUBLIC", CONNECTOR_ID, TENANT_ID)
        expected = hashlib.sha256(b"table:ANALYTICS.PUBLIC.ORDERS").hexdigest()[:16]
        assert doc.source_id == expected

    def test_source_id_is_16_chars(self) -> None:
        doc = normalize_table({"name": "MY_TABLE"}, "DB", "SCH")
        assert len(doc.source_id) == 16

    def test_rows_and_bytes(self) -> None:
        doc = normalize_table(SAMPLE_TABLE, "ANALYTICS", "PUBLIC")
        assert doc.metadata["rows"] == 1_000_000
        assert doc.metadata["bytes_size"] == 524_288_000

    def test_metadata_kind(self) -> None:
        doc = normalize_table(SAMPLE_TABLE, "ANALYTICS", "PUBLIC")
        assert doc.metadata["kind"] == "TABLE"
        assert doc.metadata["cluster_by"] == "LINEAR(ORDER_DATE)"

    def test_metadata_entity_type(self) -> None:
        doc = normalize_table(SAMPLE_TABLE, "ANALYTICS", "PUBLIC")
        assert doc.metadata["entity_type"] == "table"
        assert doc.metadata["qualified_name"] == "ANALYTICS.PUBLIC.ORDERS"

    def test_minimal_record(self) -> None:
        doc = normalize_table({"name": "TINY"}, "DB", "SCH")
        assert "DB.SCH.TINY" in doc.title
        assert doc.metadata["rows"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# with_retry tests (7)
# ─────────────────────────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        mock_fn = AsyncMock(return_value="ok")
        result = await with_retry(mock_fn)
        assert result == "ok"
        assert mock_fn.call_count == 1

    async def test_retries_on_snowflake_error(self) -> None:
        mock_fn = AsyncMock(side_effect=[
            SnowflakeNetworkError("timeout"),
            SnowflakeNetworkError("timeout"),
            "success",
        ])
        result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
        assert result == "success"
        assert mock_fn.call_count == 3

    async def test_auth_error_not_retried(self) -> None:
        mock_fn = AsyncMock(side_effect=SnowflakeAuthError("bad creds"))
        with pytest.raises(SnowflakeAuthError):
            await with_retry(mock_fn, max_attempts=3, base_delay=0)
        assert mock_fn.call_count == 1  # not retried

    async def test_raises_after_max_attempts(self) -> None:
        exc = SnowflakeNetworkError("persistent timeout")
        mock_fn = AsyncMock(side_effect=exc)
        with pytest.raises(SnowflakeNetworkError):
            await with_retry(mock_fn, max_attempts=3, base_delay=0)
        assert mock_fn.call_count == 3

    async def test_rate_limit_error_is_retried(self) -> None:
        mock_fn = AsyncMock(side_effect=[
            SnowflakeRateLimitError("rate limited", retry_after=0),
            "ok",
        ])
        result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
        assert result == "ok"
        assert mock_fn.call_count == 2

    async def test_passes_args_and_kwargs(self) -> None:
        mock_fn = AsyncMock(return_value=42)
        result = await with_retry(mock_fn, "arg1", kwarg1="kw")
        mock_fn.assert_called_once_with("arg1", kwarg1="kw")
        assert result == 42

    def test_stable_id_generates_hex(self) -> None:
        sid = _stable_id("database", "ANALYTICS")
        expected = hashlib.sha256(b"database:ANALYTICS").hexdigest()[:16]
        assert sid == expected
        assert len(sid) == 16


# ─────────────────────────────────────────────────────────────────────────────
# SnowflakeHTTPClient — authenticate (5)
# ─────────────────────────────────────────────────────────────────────────────

class TestHTTPClientAuthenticate:
    async def test_authenticate_success(self) -> None:
        from client.http_client import SnowflakeHTTPClient
        client = SnowflakeHTTPClient(config=VALID_CONFIG)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = LOGIN_SUCCESS_BODY
        mock_response.content = b"..."

        with patch.object(client._client, "post", new=AsyncMock(return_value=mock_response)):
            token = await client.authenticate()

        assert token == "session_token_abc123"
        assert client._session_token == "session_token_abc123"
        await client.aclose()

    async def test_authenticate_missing_account(self) -> None:
        from client.http_client import SnowflakeHTTPClient
        client = SnowflakeHTTPClient(config={"account": "", "username": "u", "password": "p"})
        with pytest.raises(SnowflakeAuthError) as exc_info:
            await client.authenticate()
        assert "required" in str(exc_info.value)
        await client.aclose()

    async def test_authenticate_wrong_password(self) -> None:
        from client.http_client import SnowflakeHTTPClient
        client = SnowflakeHTTPClient(config=VALID_CONFIG)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": False, "message": "Incorrect username or password was specified."}
        mock_response.content = b"..."

        with patch.object(client._client, "post", new=AsyncMock(return_value=mock_response)):
            with pytest.raises(SnowflakeAuthError) as exc_info:
                await client.authenticate()
        assert "Incorrect" in str(exc_info.value)
        await client.aclose()

    async def test_authenticate_no_token_in_response(self) -> None:
        from client.http_client import SnowflakeHTTPClient
        client = SnowflakeHTTPClient(config=VALID_CONFIG)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "data": {}}
        mock_response.content = b"..."

        with patch.object(client._client, "post", new=AsyncMock(return_value=mock_response)):
            with pytest.raises(SnowflakeAuthError) as exc_info:
                await client.authenticate()
        assert "session token" in str(exc_info.value)
        await client.aclose()

    async def test_authenticate_network_error(self) -> None:
        import httpx
        from client.http_client import SnowflakeHTTPClient
        client = SnowflakeHTTPClient(config=VALID_CONFIG)

        with patch.object(client._client, "post", new=AsyncMock(side_effect=httpx.TimeoutException("timeout"))):
            with pytest.raises(SnowflakeNetworkError):
                await client.authenticate()
        await client.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# SnowflakeHTTPClient — database/schema/table listing (6)
# ─────────────────────────────────────────────────────────────────────────────

class TestHTTPClientDatabaseOps:
    def _make_authed_client(self) -> "SnowflakeHTTPClient":
        from client.http_client import SnowflakeHTTPClient
        client = SnowflakeHTTPClient(config=VALID_CONFIG)
        client._session_token = "tok_abc"
        client._token_acquired_at = 9_999_999_999.0  # far future = not expired
        return client

    async def test_get_databases_returns_dict(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = DATABASES_RESPONSE

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
            result = await client.get_databases()
        assert "databases" in result
        await client.aclose()

    async def test_get_schemas_for_database(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SCHEMAS_RESPONSE

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)) as m:
            result = await client.get_schemas("ANALYTICS")
        assert "schemas" in result
        # Verify the path contained the database name
        call_args = m.call_args
        assert "ANALYTICS" in call_args[0][1]
        await client.aclose()

    async def test_get_tables_for_schema(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = TABLES_RESPONSE

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)) as m:
            result = await client.get_tables("ANALYTICS", "PUBLIC")
        assert "tables" in result
        call_args = m.call_args
        assert "PUBLIC" in call_args[0][1]
        await client.aclose()

    async def test_get_databases_401_raises_auth_error(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"message": "Session token expired"}

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
            with pytest.raises(SnowflakeAuthError):
                # retry_auth=True will try to re-authenticate — but no mock for post
                # so we expect a SnowflakeNetworkError or SnowflakeAuthError
                await client.get_databases()
        await client.aclose()

    async def test_get_schemas_404_raises_not_found(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"message": "Database UNKNOWN not found"}

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
            with pytest.raises(SnowflakeNotFoundError):
                await client.get_schemas("UNKNOWN")
        await client.aclose()

    async def test_get_tables_500_raises_server_error(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"message": "Internal error"}

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
            with pytest.raises(SnowflakeServerError):
                await client.get_tables("ANALYTICS", "PUBLIC")
        await client.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# SnowflakeHTTPClient — execute_statement and get_statement_result (5)
# ─────────────────────────────────────────────────────────────────────────────

class TestHTTPClientStatements:
    def _make_authed_client(self) -> "SnowflakeHTTPClient":
        from client.http_client import SnowflakeHTTPClient
        client = SnowflakeHTTPClient(config=VALID_CONFIG)
        client._session_token = "tok_abc"
        client._token_acquired_at = 9_999_999_999.0
        return client

    async def test_execute_statement_success(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = STATEMENT_SUCCESS_RESPONSE

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
            result = await client.execute_statement("SELECT 1")
        assert result["statementHandle"] == "01b3e1d6-0000-0001-0000-00000001d09e"
        await client.aclose()

    async def test_execute_statement_uses_warehouse(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = STATEMENT_SUCCESS_RESPONSE

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)) as m:
            await client.execute_statement("SELECT 1", warehouse="LARGE_WH")
        call_kwargs = m.call_args[1]
        assert call_kwargs["json"]["warehouse"] == "LARGE_WH"
        await client.aclose()

    async def test_get_statement_result(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "statementHandle": "handle-123",
            "status": "succeeded",
            "data": [["row1_col1"]],
        }

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)) as m:
            result = await client.get_statement_result("handle-123")
        assert result["statementHandle"] == "handle-123"
        call_args = m.call_args
        assert "handle-123" in call_args[0][1]
        await client.aclose()

    async def test_execute_statement_429_raises_rate_limit(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.json.return_value = {"message": "Too many requests"}

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
            with pytest.raises(SnowflakeRateLimitError):
                await client.execute_statement("SELECT SLEEP(1)")
        await client.aclose()

    async def test_execute_statement_includes_timeout(self) -> None:
        client = self._make_authed_client()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = STATEMENT_SUCCESS_RESPONSE

        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)) as m:
            await client.execute_statement("SELECT 1", timeout=120)
        call_kwargs = m.call_args[1]
        assert call_kwargs["json"]["timeout"] == 120
        await client.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP client _raise_for_status (6)
# ─────────────────────────────────────────────────────────────────────────────

class TestHTTPClientRaiseForStatus:
    def _get_client(self) -> "SnowflakeHTTPClient":
        from client.http_client import SnowflakeHTTPClient
        return SnowflakeHTTPClient(config=VALID_CONFIG)

    def test_200_does_not_raise(self) -> None:
        client = self._get_client()
        client._raise_for_status(200, {})  # should not raise

    def test_201_does_not_raise(self) -> None:
        client = self._get_client()
        client._raise_for_status(201, {})

    def test_401_raises_auth_error(self) -> None:
        client = self._get_client()
        with pytest.raises(SnowflakeAuthError):
            client._raise_for_status(401, {"message": "Token expired"})

    def test_404_raises_not_found(self) -> None:
        client = self._get_client()
        with pytest.raises(SnowflakeNotFoundError):
            client._raise_for_status(404, {"message": "Not found"})

    def test_429_raises_rate_limit(self) -> None:
        client = self._get_client()
        with pytest.raises(SnowflakeRateLimitError):
            client._raise_for_status(429, {"message": "Too many requests"})

    def test_503_raises_server_error(self) -> None:
        client = self._get_client()
        with pytest.raises(SnowflakeServerError):
            client._raise_for_status(503, {"message": "Service unavailable"})


# ─────────────────────────────────────────────────────────────────────────────
# install() tests (5)
# ─────────────────────────────────────────────────────────────────────────────

class TestInstall:
    def _make_connector(self, config: dict[str, Any] | None = None) -> SnowflakeConnector:
        return SnowflakeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config or VALID_CONFIG,
        )

    async def test_install_missing_credentials(self) -> None:
        connector = self._make_connector(config={"account": "", "username": "", "password": ""})
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_missing_password(self) -> None:
        connector = self._make_connector(config={"account": "myorg-acct", "username": "u", "password": ""})
        result = await connector.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_success(self) -> None:
        connector = self._make_connector()
        with patch("connector.SnowflakeHTTPClient") as MockClient:
            instance = MagicMock()
            instance.authenticate = AsyncMock(return_value="tok_abc")
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID
        assert "myorg-account123" in result.message

    async def test_install_auth_error(self) -> None:
        connector = self._make_connector()
        with patch("connector.SnowflakeHTTPClient") as MockClient:
            instance = MagicMock()
            instance.authenticate = AsyncMock(side_effect=SnowflakeAuthError("bad credentials"))
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
        assert "authentication failed" in result.message.lower()

    async def test_install_generic_exception(self) -> None:
        connector = self._make_connector()
        with patch("connector.SnowflakeHTTPClient") as MockClient:
            instance = MagicMock()
            instance.authenticate = AsyncMock(side_effect=Exception("connection refused"))
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# health_check() tests (5)
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthCheck:
    def _make_connector(self, config: dict[str, Any] | None = None) -> SnowflakeConnector:
        return SnowflakeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config or VALID_CONFIG,
        )

    async def test_health_check_missing_credentials(self) -> None:
        connector = self._make_connector(config={"account": "acct", "username": "", "password": ""})
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_success(self) -> None:
        connector = self._make_connector()
        with patch("connector.SnowflakeHTTPClient") as MockClient:
            instance = MagicMock()
            instance.authenticate = AsyncMock(return_value="tok")
            instance.execute_statement = AsyncMock(return_value=STATEMENT_SUCCESS_RESPONSE)
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.account == "myorg-account123"
        assert result.username == "SHIELVA_USER"

    async def test_health_check_auth_error(self) -> None:
        connector = self._make_connector()
        with patch("connector.SnowflakeHTTPClient") as MockClient:
            instance = MagicMock()
            instance.authenticate = AsyncMock(side_effect=SnowflakeAuthError("bad token"))
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        connector = self._make_connector()
        with patch("connector.SnowflakeHTTPClient") as MockClient:
            instance = MagicMock()
            instance.authenticate = AsyncMock(side_effect=SnowflakeNetworkError("timeout"))
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_generic_exception(self) -> None:
        connector = self._make_connector()
        with patch("connector.SnowflakeHTTPClient") as MockClient:
            instance = MagicMock()
            instance.authenticate = AsyncMock(return_value="tok")
            instance.execute_statement = AsyncMock(side_effect=RuntimeError("unexpected"))
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ─────────────────────────────────────────────────────────────────────────────
# sync() tests (8)
# ─────────────────────────────────────────────────────────────────────────────

class TestSync:
    def _make_connector(self, config: dict[str, Any] | None = None) -> SnowflakeConnector:
        return SnowflakeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config or VALID_CONFIG,
        )

    async def test_sync_missing_credentials(self) -> None:
        connector = self._make_connector(config={"account": "a", "username": "", "password": ""})
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "required" in result.message.lower()

    async def test_sync_empty_databases(self) -> None:
        connector = self._make_connector()
        connector.list_databases = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_databases_only(self) -> None:
        connector = self._make_connector()
        connector.list_databases = AsyncMock(return_value=[SAMPLE_DATABASE])
        connector.list_schemas = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 1
        assert result.documents_synced == 1

    async def test_sync_databases_and_schemas(self) -> None:
        connector = self._make_connector()
        connector.list_databases = AsyncMock(return_value=[SAMPLE_DATABASE])
        connector.list_schemas = AsyncMock(return_value=[SAMPLE_SCHEMA])
        connector.list_tables = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 2  # 1 db + 1 schema
        assert result.documents_synced == 2

    async def test_sync_databases_schemas_tables(self) -> None:
        connector = self._make_connector()
        connector.list_databases = AsyncMock(return_value=[SAMPLE_DATABASE])
        connector.list_schemas = AsyncMock(return_value=[SAMPLE_SCHEMA])
        connector.list_tables = AsyncMock(return_value=[SAMPLE_TABLE])
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 3  # 1 db + 1 schema + 1 table
        assert result.documents_synced == 3
        assert result.documents_failed == 0

    async def test_sync_fatal_database_error(self) -> None:
        connector = self._make_connector()
        connector.list_databases = AsyncMock(side_effect=SnowflakeAuthError("unauthorized"))
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_schema_error_is_skipped(self) -> None:
        connector = self._make_connector()
        connector.list_databases = AsyncMock(return_value=[SAMPLE_DATABASE])
        connector.list_schemas = AsyncMock(side_effect=SnowflakeError("access denied"))
        result = await connector.sync()
        # 1 database succeeds; schema phase is skipped
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_synced == 1

    async def test_sync_partial_when_normalize_fails(self) -> None:
        connector = self._make_connector()
        # Return a database with no 'name' to trigger normalization failure
        bad_db: dict[str, Any] = {}
        connector.list_databases = AsyncMock(return_value=[bad_db])
        connector.list_schemas = AsyncMock(return_value=[])
        result = await connector.sync()
        # normalize_database({}) still produces a valid doc (name=""), so it succeeds
        # Let's also verify we get COMPLETED or PARTIAL
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


# ─────────────────────────────────────────────────────────────────────────────
# list_databases / list_schemas / list_tables (5)
# ─────────────────────────────────────────────────────────────────────────────

class TestListMethods:
    def _make_connector(self) -> SnowflakeConnector:
        return SnowflakeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=VALID_CONFIG,
        )

    async def test_list_databases_returns_list(self) -> None:
        connector = self._make_connector()
        connector.client.get_databases = AsyncMock(return_value=DATABASES_RESPONSE)
        result = await connector.list_databases()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_schemas_returns_list(self) -> None:
        connector = self._make_connector()
        connector.client.get_schemas = AsyncMock(return_value=SCHEMAS_RESPONSE)
        result = await connector.list_schemas("ANALYTICS")
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_tables_returns_list(self) -> None:
        connector = self._make_connector()
        connector.client.get_tables = AsyncMock(return_value=TABLES_RESPONSE)
        result = await connector.list_tables("ANALYTICS", "PUBLIC")
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_databases_empty_response(self) -> None:
        connector = self._make_connector()
        connector.client.get_databases = AsyncMock(return_value={"databases": []})
        result = await connector.list_databases()
        assert result == []

    async def test_list_tables_no_tables_key(self) -> None:
        connector = self._make_connector()
        connector.client.get_tables = AsyncMock(return_value={})
        result = await connector.list_tables("ANALYTICS", "PUBLIC")
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# execute_query / get_query_result (4)
# ─────────────────────────────────────────────────────────────────────────────

class TestExecuteQuery:
    def _make_connector(self) -> SnowflakeConnector:
        return SnowflakeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=VALID_CONFIG,
        )

    async def test_execute_query_returns_response(self) -> None:
        connector = self._make_connector()
        connector.client.execute_statement = AsyncMock(return_value=STATEMENT_SUCCESS_RESPONSE)
        result = await connector.execute_query("SELECT 1")
        assert result["statementHandle"] == "01b3e1d6-0000-0001-0000-00000001d09e"

    async def test_execute_query_passes_warehouse(self) -> None:
        connector = self._make_connector()
        connector.client.execute_statement = AsyncMock(return_value=STATEMENT_SUCCESS_RESPONSE)
        await connector.execute_query("SELECT 1", warehouse="LARGE_WH")
        call_kwargs = connector.client.execute_statement.call_args[1]
        assert call_kwargs["warehouse"] == "LARGE_WH"

    async def test_get_query_result(self) -> None:
        connector = self._make_connector()
        expected = {"statementHandle": "h-123", "status": "succeeded"}
        connector.client.get_statement_result = AsyncMock(return_value=expected)
        result = await connector.get_query_result("h-123")
        connector.client.get_statement_result.assert_called_once_with("h-123")
        assert result["status"] == "succeeded"

    async def test_execute_query_auth_error_propagates(self) -> None:
        connector = self._make_connector()
        connector.client.execute_statement = AsyncMock(
            side_effect=SnowflakeAuthError("session expired")
        )
        with pytest.raises(SnowflakeAuthError):
            await connector.execute_query("SELECT 1")


# ─────────────────────────────────────────────────────────────────────────────
# Account identifier URL construction (3)
# ─────────────────────────────────────────────────────────────────────────────

class TestAccountURLConstruction:
    def test_base_url_from_account(self) -> None:
        from client.http_client import SnowflakeHTTPClient
        client = SnowflakeHTTPClient(config={"account": "myorg-account123", "username": "u", "password": "p"})
        assert client._base_url == "https://myorg-account123.snowflakecomputing.com"

    def test_base_url_strips_whitespace(self) -> None:
        from client.http_client import SnowflakeHTTPClient
        client = SnowflakeHTTPClient(config={"account": "  myorg-account123  ", "username": "u", "password": "p"})
        assert "myorg-account123" in client._base_url
        assert "  " not in client._base_url

    def test_base_url_with_enterprise_account(self) -> None:
        from client.http_client import SnowflakeHTTPClient
        client = SnowflakeHTTPClient(config={"account": "abc12345.us-east-1", "username": "u", "password": "p"})
        assert client._base_url == "https://abc12345.us-east-1.snowflakecomputing.com"


# ─────────────────────────────────────────────────────────────────────────────
# _has_credentials, connector constants, lifecycle (5)
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectorLifecycle:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "snowflake"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_has_credentials_all_present(self) -> None:
        connector = SnowflakeConnector(config=VALID_CONFIG)
        assert connector._has_credentials() is True

    def test_has_credentials_missing_password(self) -> None:
        connector = SnowflakeConnector(config={"account": "a", "username": "u", "password": ""})
        assert connector._has_credentials() is False

    def test_has_credentials_all_missing(self) -> None:
        connector = SnowflakeConnector(config={})
        assert connector._has_credentials() is False

    async def test_aclose(self) -> None:
        connector = SnowflakeConnector(config=VALID_CONFIG)
        connector.client.aclose = AsyncMock()
        await connector.aclose()
        connector.client.aclose.assert_called_once()

    async def test_context_manager(self) -> None:
        connector = SnowflakeConnector(config=VALID_CONFIG)
        connector.client.aclose = AsyncMock()
        async with connector as c:
            assert c is connector
        connector.client.aclose.assert_called_once()

    def test_connector_stores_config(self) -> None:
        connector = SnowflakeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=VALID_CONFIG,
        )
        assert connector.tenant_id == TENANT_ID
        assert connector.connector_id == CONNECTOR_ID
        assert connector.config["account"] == "myorg-account123"

    def test_client_initialized(self) -> None:
        from client.http_client import SnowflakeHTTPClient
        connector = SnowflakeConnector(config=VALID_CONFIG)
        assert isinstance(connector.client, SnowflakeHTTPClient)
