from __future__ import annotations

from typing import Any

from client.http_client import SnowflakeHTTPClient
from exceptions import SnowflakeAuthError, SnowflakeError, SnowflakeNetworkError
from helpers.utils import (
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
    SyncResult,
    SyncStatus,
)

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config: dict[str, Any] = config or {}

CONNECTOR_TYPE = "snowflake"
AUTH_TYPE = "api_key"

# SQL used for health check — lightweight, always succeeds when auth is valid
_HEALTH_CHECK_SQL = "SELECT CURRENT_USER(), CURRENT_ACCOUNT(), CURRENT_WAREHOUSE()"

# SQL for query history metadata sync
_QUERY_HISTORY_SQL = (
    "SELECT QUERY_ID, QUERY_TEXT, DATABASE_NAME, SCHEMA_NAME, QUERY_TYPE, "
    "USER_NAME, WAREHOUSE_NAME, EXECUTION_STATUS, ERROR_CODE, ERROR_MESSAGE, "
    "START_TIME, END_TIME, TOTAL_ELAPSED_TIME, ROWS_PRODUCED "
    "FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => 100)) "
    "ORDER BY START_TIME DESC"
)


class SnowflakeConnector(BaseConnector):
    """
    Shielva connector for Snowflake data warehouse.

    Provides authentication (username/password via SQL API session),
    health checks, full sync of databases + schemas + tables,
    direct SQL query execution, and query history retrieval.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self.client = SnowflakeHTTPClient(config=_config)

    def _has_credentials(self) -> bool:
        """Return True when account, username, and password are all present."""
        cfg = self.config
        return bool(
            cfg.get("account", "").strip()
            and cfg.get("username", "").strip()
            and cfg.get("password", "").strip()
        )

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """
        Validate Snowflake credentials by authenticating via the SQL API.

        Returns HEALTHY + CONNECTED on success.
        Returns OFFLINE + MISSING_CREDENTIALS when required fields are absent.
        Returns OFFLINE + INVALID_CREDENTIALS on auth failure.
        """
        if not self._has_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="account, username, and password are required",
            )

        client = SnowflakeHTTPClient(config=self.config)
        try:
            await client.authenticate()
            account: str = self.config.get("account", "")
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                account=account,
                message=f"Connected to Snowflake account: {account}",
            )
        except SnowflakeAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Snowflake authentication failed: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """
        Authenticate and execute SELECT CURRENT_USER() to verify connectivity.

        Returns HEALTHY, DEGRADED, or OFFLINE with structured status.
        """
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="account, username, and password are required",
            )

        client = SnowflakeHTTPClient(config=self.config)
        try:
            await client.authenticate()
            result = await client.execute_statement(_HEALTH_CHECK_SQL)
            await client.aclose()

            # Extract username from result data rows
            username: str = ""
            account: str = self.config.get("account", "")
            rows: list[Any] = (
                result.get("data", [])
                or result.get("resultSetMetaData", {}).get("data", [])
                or []
            )
            if rows and isinstance(rows[0], list) and len(rows[0]) >= 1:
                username = str(rows[0][0]) if rows[0][0] else ""

            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                account=account,
                username=username,
                message=f"Snowflake API is reachable. Account: {account}",
            )
        except SnowflakeAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except SnowflakeNetworkError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(self, kb_id: str = "", **kwargs: Any) -> SyncResult:
        """
        Sync Snowflake databases, schemas (as metadata), and tables into the knowledge base.

        Phase 1: List all accessible databases → normalize → ingest.
        Phase 2: For each database, list schemas → normalize → ingest.
        Phase 3: For each schema, list tables → normalize → ingest.

        Returns SyncStatus.COMPLETED when all phases succeed with zero failures,
        SyncStatus.PARTIAL when some records fail, SyncStatus.FAILED on fatal error.
        """
        if not self._has_credentials():
            return SyncResult(
                status=SyncStatus.FAILED,
                message="account, username, and password are required",
            )

        found = 0
        synced = 0
        failed = 0

        # Phase 1 — databases
        try:
            databases = await self.list_databases()
        except SnowflakeError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message=str(exc),
            )

        found += len(databases)
        for db_raw in databases:
            try:
                doc = normalize_database(db_raw, self.connector_id, self.tenant_id)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # Phase 2 + 3 — schemas and tables per database
        for db_raw in databases:
            db_name: str = db_raw.get("name", "") or db_raw.get("NAME", "") or ""
            if not db_name:
                continue

            # Schemas
            schemas: list[dict[str, Any]] = []
            try:
                schemas = await self.list_schemas(db_name)
                found += len(schemas)
                for schema_raw in schemas:
                    try:
                        doc = normalize_schema(schema_raw, db_name, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except SnowflakeError:
                # Skip databases we cannot access
                pass

            # Tables per schema
            for schema_raw in schemas:
                schema_name: str = schema_raw.get("name", "") or schema_raw.get("NAME", "") or ""
                if not schema_name:
                    continue
                try:
                    tables = await self.list_tables(db_name, schema_name)
                    found += len(tables)
                    for table_raw in tables:
                        try:
                            doc = normalize_table(
                                table_raw, db_name, schema_name,
                                self.connector_id, self.tenant_id,
                            )
                            if kb_id:
                                await self._ingest_document(doc, kb_id)
                            synced += 1
                        except Exception:
                            failed += 1
                except SnowflakeError:
                    pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Database metadata ─────────────────────────────────────────────────────

    async def list_databases(self) -> list[dict[str, Any]]:
        """GET /api/v2/databases — list all accessible Snowflake databases."""
        response = await with_retry(self.client.get_databases)
        # Snowflake wraps list responses: {"databases": [...]} or top-level list
        if isinstance(response, dict):
            return response.get("databases", []) or []
        return []

    async def list_schemas(self, database: str) -> list[dict[str, Any]]:
        """GET /api/v2/databases/{database}/schemas — list schemas in a database."""
        response = await with_retry(self.client.get_schemas, database)
        if isinstance(response, dict):
            return response.get("schemas", []) or []
        return []

    async def list_tables(self, database: str, schema: str) -> list[dict[str, Any]]:
        """GET /api/v2/databases/{database}/schemas/{schema}/tables — list tables."""
        response = await with_retry(self.client.get_tables, database, schema)
        if isinstance(response, dict):
            return response.get("tables", []) or []
        return []

    # ── SQL execution ─────────────────────────────────────────────────────────

    async def execute_query(
        self,
        sql: str,
        warehouse: str | None = None,
        database: str | None = None,
        schema: str | None = None,
        role: str | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """
        POST /api/v2/statements — execute a SQL statement and return the result.

        For long-running queries, Snowflake returns a statementHandle that can be
        polled via get_statement_result().
        """
        return await with_retry(
            self.client.execute_statement,
            sql,
            warehouse=warehouse,
            database=database,
            schema=schema,
            role=role,
            timeout=timeout,
        )

    async def get_query_result(self, statement_handle: str) -> dict[str, Any]:
        """GET /api/v2/statements/{handle} — poll for an async statement result."""
        return await with_retry(self.client.get_statement_result, statement_handle)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def __aenter__(self) -> SnowflakeConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
