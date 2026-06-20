from __future__ import annotations

from typing import Any, Dict

from client import AirtableHTTPClient
from exceptions import AirtableAuthError, AirtableError, AirtableNetworkError
from helpers import normalize_record, with_retry
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
        def __init__(self, tenant_id: str = "", connector_id: str = "", config: Dict[str, Any] | None = None) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

SYNC_PAGE_SIZE: int = 100
CONNECTOR_TYPE: str = "airtable"
AUTH_TYPE: str = "api_key"


class AirtableConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Airtable.

    Syncs all bases, tables, and records from the Airtable REST API v0
    using a Personal Access Token (Bearer auth).
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
        # Convenience keyword args for standalone / test usage
        api_key: str = "",
        access_token: str = "",
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._tenant_id: str = tenant_id

        # Support both 'api_key' (spec) and 'access_token' (legacy) config keys
        self._api_key: str = (
            _config.get("api_key", "")
            or _config.get("access_token", "")
            or api_key
            or access_token
        )
        self._base_id: str = _config.get("base_id", "")
        self._http_client: AirtableHTTPClient | None = None

    def _make_client(self) -> AirtableHTTPClient:
        return AirtableHTTPClient()

    def _ensure_client(self) -> AirtableHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._api_key:
            missing.append("api_key")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the api_key by calling GET /meta/whoami."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.whoami, self._api_key)
            user_id: str = data.get("id", "")
            email: str = data.get("email", "")
            display_name: str = email or user_id or "Airtable user"
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Airtable as {display_name}",
            )
        except AirtableAuthError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /meta/whoami and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.whoami, self._api_key)
            email: str = data.get("email", "")
            user_id: str = data.get("id", "")
            display_name = email or user_id or "Airtable user"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Airtable API reachable. User: {display_name}",
            )
        except AirtableAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except AirtableNetworkError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(self, kb_id: str = "") -> SyncResult:
        """Sync all bases → tables → records into the knowledge base.

        Iterates every accessible base, fetches its tables, then paginates
        all records in each table. Each record is normalized to a
        ConnectorDocument and optionally ingested via _ingest_document().
        """
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        try:
            bases = await self._fetch_all_bases(client)
        except AirtableError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        for base in bases:
            base_id: str = base.get("id", "")
            base_name: str = base.get("name", base_id)

            try:
                tables_data = await with_retry(
                    client.list_tables, self._api_key, base_id
                )
            except AirtableError:
                failed += 1
                continue

            tables: list[dict[str, Any]] = tables_data.get("tables", [])

            for table in tables:
                table_name: str = table.get("name", "")
                if not table_name:
                    continue

                offset: str | None = None
                while True:
                    try:
                        page_data = await with_retry(
                            client.list_records,
                            self._api_key,
                            base_id,
                            table_name,
                            SYNC_PAGE_SIZE,
                            offset,
                        )
                    except AirtableError:
                        failed += 1
                        break

                    records: list[dict[str, Any]] = page_data.get("records", [])
                    found += len(records)

                    for record in records:
                        try:
                            doc = normalize_record(
                                record,
                                base_id,
                                base_name,
                                table_name,
                                self.connector_id,
                                self._tenant_id,
                            )
                            if kb_id:
                                await self._ingest_document(doc, kb_id)
                            synced += 1
                        except Exception:
                            failed += 1

                    offset = page_data.get("offset")
                    if not offset or not records:
                        break

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_bases(
        self, client: AirtableHTTPClient
    ) -> list[dict[str, Any]]:
        """Paginate through all bases and return a flat list."""
        all_bases: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            data = await with_retry(
                client.list_bases, self._api_key, offset
            )
            all_bases.extend(data.get("bases", []))
            offset = data.get("offset")
            if not offset:
                break
        return all_bases

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Bases ─────────────────────────────────────────────────────────────────

    async def list_bases(self, offset: str | None = None) -> dict[str, Any]:
        """Return a (possibly paginated) list of Airtable bases."""
        client = self._ensure_client()
        return await with_retry(client.list_bases, self._api_key, offset)

    # ── Tables ────────────────────────────────────────────────────────────────

    async def list_tables(self, base_id: str) -> dict[str, Any]:
        """Return all tables for the given base."""
        client = self._ensure_client()
        return await with_retry(client.list_tables, self._api_key, base_id)

    # ── Records ───────────────────────────────────────────────────────────────

    async def list_records(
        self,
        base_id: str,
        table_name: str,
        page_size: int = 100,
        offset: str | None = None,
    ) -> dict[str, Any]:
        """Return a page of records from the given table."""
        client = self._ensure_client()
        return await with_retry(
            client.list_records,
            self._api_key,
            base_id,
            table_name,
            page_size,
            offset,
        )

    async def get_record(
        self, base_id: str, table_name: str, record_id: str
    ) -> dict[str, Any]:
        """Return a single record by ID."""
        client = self._ensure_client()
        return await with_retry(
            client.get_record,
            self._api_key,
            base_id,
            table_name,
            record_id,
        )

    # ── Views ─────────────────────────────────────────────────────────────────

    async def list_views(self, base_id: str, table_id: str) -> dict[str, Any]:
        """Return all views for the given table in the given base."""
        client = self._ensure_client()
        return await with_retry(client.list_views, self._api_key, base_id, table_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> AirtableConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
