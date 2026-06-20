"""Coda connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

try:
    from shared.base_connector import (
        AuthStatus,
        BaseConnector,
        ConnectorHealth,
        ConnectorStatus,
        NormalizedDocument,
        SyncResult,
        SyncStatus,
    )
    _BASE = BaseConnector
    _HAS_SDK = True
except ImportError:
    _BASE = object  # type: ignore[assignment,misc]
    _HAS_SDK = False

from client.http_client import CodaHTTPClient
from exceptions import CodaAuthError, CodaError, CodaNetworkError, CodaNotFoundError
from helpers.utils import normalize_doc, normalize_page, normalize_table, normalize_row, with_retry
from models import (
    AuthStatus as _LocalAuthStatus,
    ConnectorHealth as _LocalConnectorHealth,
    ConnectorDocument,
    InstallResult,
    HealthCheckResult,
    SyncResult as _LocalSyncResult,
    SyncStatus as _LocalSyncStatus,
)

logger = structlog.get_logger(__name__)

CONNECTOR_TYPE = "coda"
AUTH_TYPE = "api_key"


class CodaConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for Coda via the Coda API v1."""

    CONNECTOR_TYPE = "coda"
    CONNECTOR_NAME = "Coda"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS = ["api_token"]

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        if _HAS_SDK:
            super().__init__(tenant_id, connector_id, cfg)
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = cfg
        self.client = CodaHTTPClient(config=self.config)

    def _get_token(self) -> str:
        return self.config.get("api_token", "")

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        """Validate that api_token is present in config."""
        api_token = self.config.get("api_token")

        if not api_token:
            logger.warning(
                "coda.install.missing_credentials",
                connector_id=self.connector_id,
            )
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,  # type: ignore[name-defined]
                    auth_status=AuthStatus.MISSING_CREDENTIALS,  # type: ignore[name-defined]
                    message="api_token is required",
                )
            return InstallResult(
                health=_LocalConnectorHealth.OFFLINE,
                auth_status=_LocalAuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="api_token is required",
            )

        logger.info("coda.install.ok", connector_id=self.connector_id)

        if _HAS_SDK:
            return ConnectorStatus(  # type: ignore[name-defined]
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                message="Connector installed — API token present",
            )
        return InstallResult(
            health=_LocalConnectorHealth.HEALTHY,
            auth_status=_LocalAuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Connector installed — API token present",
        )

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        """Call GET /whoami to validate the API token and retrieve the user name."""
        try:
            data = await with_retry(
                lambda: self.client.get_who_am_i(),
                max_attempts=2,
            )
            login_id = data.get("loginId", "")
            name = data.get("name", login_id or "unknown")
            msg = f"Connected — user: {name}"

            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                    auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                    message=msg,
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.HEALTHY,
                auth_status=_LocalAuthStatus.CONNECTED,
                message=msg,
            )
        except CodaAuthError as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.INVALID_CREDENTIALS,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.FAILED,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.FAILED,
                message=str(exc),
            )

    # ── sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: Optional[Any] = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> Any:
        """Sync all accessible docs, pages, tables, and rows.

        Walk: docs → pages (per doc) + tables (per doc) → rows (per table).
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            docs = await self.list_docs()
            for raw_doc in docs:
                doc_id = raw_doc.get("id", "")

                # Normalize and count the doc itself
                try:
                    doc_document = normalize_doc(raw_doc)
                    documents_found += 1

                    if _HAS_SDK:
                        normalized = NormalizedDocument(  # type: ignore[name-defined]
                            id=doc_document.id,
                            source_id=doc_id,
                            title=doc_document.title,
                            content=doc_document.content,
                            content_type="text",
                            source_url=raw_doc.get("browserLink", ""),
                            author=raw_doc.get("ownerName", ""),
                            source="coda",
                            tenant_id=self.tenant_id,
                            connector_id=self.connector_id,
                            metadata=doc_document.metadata,
                        )
                        await self.ingest_document(normalized, kb_id=kb_id or "")

                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "coda.sync.doc_failed",
                        doc_id=doc_id,
                        error=str(exc),
                    )
                    documents_failed += 1
                    continue

                # Pages within the doc
                try:
                    pages = await self.list_pages(doc_id)
                    for raw_page in pages:
                        page_id = raw_page.get("id", "")
                        try:
                            page_document = normalize_page(raw_page, doc_id)
                            documents_found += 1

                            if _HAS_SDK:
                                normalized = NormalizedDocument(  # type: ignore[name-defined]
                                    id=page_document.id,
                                    source_id=page_id,
                                    title=page_document.title,
                                    content=page_document.content,
                                    content_type="text",
                                    source_url=raw_page.get("browserLink", ""),
                                    author="",
                                    source="coda",
                                    tenant_id=self.tenant_id,
                                    connector_id=self.connector_id,
                                    metadata=page_document.metadata,
                                )
                                await self.ingest_document(normalized, kb_id=kb_id or "")

                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "coda.sync.page_failed",
                                page_id=page_id,
                                doc_id=doc_id,
                                error=str(exc),
                            )
                            documents_failed += 1
                except Exception as exc:
                    logger.warning(
                        "coda.sync.pages_list_failed",
                        doc_id=doc_id,
                        error=str(exc),
                    )

                # Tables within the doc
                try:
                    tables = await self.list_tables(doc_id)
                    for raw_table in tables:
                        table_id = raw_table.get("id", "")
                        try:
                            table_document = normalize_table(raw_table, doc_id)
                            documents_found += 1

                            if _HAS_SDK:
                                normalized = NormalizedDocument(  # type: ignore[name-defined]
                                    id=table_document.id,
                                    source_id=table_id,
                                    title=table_document.title,
                                    content=table_document.content,
                                    content_type="text",
                                    source_url=raw_table.get("browserLink", ""),
                                    author="",
                                    source="coda",
                                    tenant_id=self.tenant_id,
                                    connector_id=self.connector_id,
                                    metadata=table_document.metadata,
                                )
                                await self.ingest_document(normalized, kb_id=kb_id or "")

                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "coda.sync.table_failed",
                                table_id=table_id,
                                doc_id=doc_id,
                                error=str(exc),
                            )
                            documents_failed += 1
                            continue

                        # Rows within the table
                        try:
                            rows = await self.list_rows(doc_id, table_id)
                            for raw_row in rows:
                                row_id = raw_row.get("id", "")
                                try:
                                    row_document = normalize_row(raw_row, doc_id, table_id)
                                    documents_found += 1

                                    if _HAS_SDK:
                                        normalized = NormalizedDocument(  # type: ignore[name-defined]
                                            id=row_document.id,
                                            source_id=row_id,
                                            title=row_document.title,
                                            content=row_document.content,
                                            content_type="text",
                                            source_url=raw_row.get("browserLink", ""),
                                            author="",
                                            source="coda",
                                            tenant_id=self.tenant_id,
                                            connector_id=self.connector_id,
                                            metadata=row_document.metadata,
                                        )
                                        await self.ingest_document(normalized, kb_id=kb_id or "")

                                    documents_synced += 1
                                except Exception as exc:
                                    logger.error(
                                        "coda.sync.row_failed",
                                        row_id=row_id,
                                        table_id=table_id,
                                        doc_id=doc_id,
                                        error=str(exc),
                                    )
                                    documents_failed += 1
                        except Exception as exc:
                            logger.warning(
                                "coda.sync.rows_list_failed",
                                table_id=table_id,
                                doc_id=doc_id,
                                error=str(exc),
                            )
                except Exception as exc:
                    logger.warning(
                        "coda.sync.tables_list_failed",
                        doc_id=doc_id,
                        error=str(exc),
                    )

            status = _LocalSyncStatus.COMPLETED if documents_failed == 0 else _LocalSyncStatus.PARTIAL
            msg = (
                f"Synced {documents_synced}/{documents_found} objects "
                f"({documents_failed} failed)"
            )

            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=msg,
                )
            return _LocalSyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=msg,
            )

        except Exception as exc:
            logger.error(
                "coda.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.FAILED,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=str(exc),
                )
            return _LocalSyncResult(
                status=_LocalSyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── list_docs ─────────────────────────────────────────────────────────────

    async def list_docs(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """List all accessible Coda docs (cursor-paginated)."""
        results: List[Dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda t=page_token: self.client.get_docs(page_token=t),
                max_attempts=3,
            )
            results.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return results

    # ── list_pages ────────────────────────────────────────────────────────────

    async def list_pages(self, doc_id: str) -> List[Dict[str, Any]]:
        """List all pages within a Coda doc (cursor-paginated)."""
        results: List[Dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda t=page_token: self.client.get_pages(doc_id, page_token=t),
                max_attempts=3,
            )
            results.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return results

    # ── list_tables ───────────────────────────────────────────────────────────

    async def list_tables(self, doc_id: str) -> List[Dict[str, Any]]:
        """List all tables within a Coda doc (cursor-paginated)."""
        results: List[Dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda t=page_token: self.client.get_tables(doc_id, page_token=t),
                max_attempts=3,
            )
            results.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return results

    # ── list_rows ─────────────────────────────────────────────────────────────

    async def list_rows(
        self,
        doc_id: str,
        table_id: str,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """List all rows within a Coda table (cursor-paginated)."""
        results: List[Dict[str, Any]] = []
        page_token: Optional[str] = None

        while True:
            resp = await with_retry(
                lambda t=page_token: self.client.get_rows(doc_id, table_id, page_token=t),
                max_attempts=3,
            )
            results.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return results

    # ── get_doc ───────────────────────────────────────────────────────────────

    async def get_doc(self, doc_id: str) -> Dict[str, Any]:
        """GET /docs/{docId} — retrieve a single Coda doc."""
        return await with_retry(
            lambda: self.client.get_doc(doc_id),
            max_attempts=3,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> "CodaConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
