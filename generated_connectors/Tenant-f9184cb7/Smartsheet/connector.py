"""Smartsheet connector — orchestration only.

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

from client.http_client import SmartsheetHTTPClient
from exceptions import SmartsheetAuthError, SmartsheetError, SmartsheetNetworkError
from helpers.utils import (
    normalize_report,
    normalize_row,
    normalize_sheet,
    normalize_workspace,
    with_retry,
)
from models import (
    AuthStatus as _LocalAuthStatus,
    ConnectorDocument,
    ConnectorHealth as _LocalConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult as _LocalSyncResult,
    SyncStatus as _LocalSyncStatus,
)

logger = structlog.get_logger(__name__)

CONNECTOR_TYPE = "smartsheet"
AUTH_TYPE = "api_key"

_DEFAULT_PAGE_SIZE = 100
_DEFAULT_ROW_PAGE_SIZE = 500


class SmartsheetConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for Smartsheet via the Smartsheet REST API 2.0."""

    CONNECTOR_TYPE = "smartsheet"
    CONNECTOR_NAME = "Smartsheet"
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
        self._http_client: Optional[SmartsheetHTTPClient] = None

    def _ensure_client(self) -> SmartsheetHTTPClient:
        if self._http_client is None:
            self._http_client = SmartsheetHTTPClient(config=self.config)
        return self._http_client

    def _get_token(self) -> str:
        return self.config.get("api_token", "")

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        """Validate that api_token is present; returns InstallResult."""
        api_token = self.config.get("api_token")

        if not api_token:
            logger.warning(
                "smartsheet.install.missing_credentials",
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

        logger.info("smartsheet.install.ok", connector_id=self.connector_id)

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
        """GET /users/me to verify the API token."""
        try:
            me = await with_retry(
                lambda: self._ensure_client().get_current_user(),
                max_attempts=2,
            )
            first_name = me.get("firstName", "") or ""
            last_name = me.get("lastName", "") or ""
            email = me.get("email", "") or ""
            full_name = f"{first_name} {last_name}".strip()
            display = full_name or email or "unknown user"
            msg = f"Connected as: {display}"

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
        except SmartsheetAuthError as exc:
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
        """Sync all sheets and their rows from Smartsheet.

        Fetches all sheets (paginated), then for each sheet fetches all rows
        (paginated). Also syncs workspaces and reports. Normalizes each
        resource to a ConnectorDocument.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # --- Sync sheets and rows ---
            sheets = await self.list_sheets()

            for sheet in sheets:
                sheet_id_raw = sheet.get("id")
                sheet_name = sheet.get("name", str(sheet_id_raw))
                if not sheet_id_raw:
                    continue

                sheet_id = int(sheet_id_raw)

                try:
                    # Normalize sheet itself
                    sheet_doc = normalize_sheet(sheet)
                    documents_found += 1
                    if _HAS_SDK:
                        normalized = NormalizedDocument(  # type: ignore[name-defined]
                            id=sheet_doc.id,
                            source_id=f"sheet:{sheet_id}",
                            title=sheet_doc.title,
                            content=sheet_doc.content,
                            content_type="text",
                            source_url=sheet.get("permalink", ""),
                            author="",
                            source="smartsheet",
                            tenant_id=self.tenant_id,
                            connector_id=self.connector_id,
                            metadata=sheet_doc.metadata,
                        )
                        await self.ingest_document(normalized, kb_id=kb_id or "")
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "smartsheet.sync.sheet_normalize_failed",
                        sheet_id=sheet_id,
                        error=str(exc),
                    )
                    documents_failed += 1

                # Sync rows for this sheet
                try:
                    rows = await self.list_rows(sheet_id=sheet_id)
                    documents_found += len(rows)

                    for row in rows:
                        try:
                            row_doc = normalize_row(row, sheet_id)
                            if _HAS_SDK:
                                normalized_row = NormalizedDocument(  # type: ignore[name-defined]
                                    id=row_doc.id,
                                    source_id=f"sheet:{sheet_id}:row:{row.get('id', '')}",
                                    title=row_doc.title,
                                    content=row_doc.content,
                                    content_type="text",
                                    source_url="",
                                    author="",
                                    source="smartsheet",
                                    tenant_id=self.tenant_id,
                                    connector_id=self.connector_id,
                                    metadata=row_doc.metadata,
                                )
                                await self.ingest_document(
                                    normalized_row, kb_id=kb_id or ""
                                )
                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "smartsheet.sync.row_failed",
                                sheet_id=sheet_id,
                                row_id=row.get("id", ""),
                                error=str(exc),
                            )
                            documents_failed += 1

                except SmartsheetAuthError:
                    raise
                except Exception as exc:
                    logger.error(
                        "smartsheet.sync.sheet_rows_failed",
                        sheet_id=sheet_id,
                        error=str(exc),
                    )
                    documents_failed += 1

            # --- Sync workspaces ---
            try:
                workspaces = await self.list_workspaces()
                documents_found += len(workspaces)
                for ws in workspaces:
                    try:
                        ws_doc = normalize_workspace(ws)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "smartsheet.sync.workspace_failed",
                            workspace_id=ws.get("id", ""),
                            error=str(exc),
                        )
                        documents_failed += 1
            except SmartsheetAuthError:
                raise
            except Exception as exc:
                logger.warning("smartsheet.sync.workspaces_skipped", error=str(exc))

            # --- Sync reports ---
            try:
                reports = await self.list_reports()
                documents_found += len(reports)
                for rpt in reports:
                    try:
                        rpt_doc = normalize_report(rpt)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "smartsheet.sync.report_failed",
                            report_id=rpt.get("id", ""),
                            error=str(exc),
                        )
                        documents_failed += 1
            except SmartsheetAuthError:
                raise
            except Exception as exc:
                logger.warning("smartsheet.sync.reports_skipped", error=str(exc))

            status = (
                _LocalSyncStatus.COMPLETED
                if documents_failed == 0
                else _LocalSyncStatus.PARTIAL
            )
            msg = (
                f"Synced {documents_synced}/{documents_found} documents "
                f"from {len(sheets)} sheets"
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
                "smartsheet.sync.failed",
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

    # ── convenience methods ───────────────────────────────────────────────────

    async def list_sheets(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """List all sheets via page-based pagination."""
        sheets: List[Dict[str, Any]] = []
        page = 1

        while True:
            response = await with_retry(
                lambda p=page: self._ensure_client().get_sheets(
                    page=p,
                    page_size=_DEFAULT_PAGE_SIZE,
                ),
                max_attempts=3,
            )
            page_data: List[Dict[str, Any]] = response.get("data") or []
            sheets.extend(page_data)

            total_pages = response.get("totalPages", 1)
            page_number = response.get("pageNumber", page)
            if page_number >= total_pages or not page_data:
                break
            page += 1

        return sheets

    async def list_rows(
        self,
        sheet_id: int,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """List all rows for a given sheet via page-based pagination."""
        rows: List[Dict[str, Any]] = []
        page = 1

        while True:
            response = await with_retry(
                lambda p=page: self._ensure_client().get_rows(
                    sheet_id=sheet_id,
                    page=p,
                    page_size=_DEFAULT_ROW_PAGE_SIZE,
                ),
                max_attempts=3,
            )
            page_data: List[Dict[str, Any]] = response.get("data") or []
            rows.extend(page_data)

            total_pages = response.get("totalPages", 1)
            page_number = response.get("pageNumber", page)
            if page_number >= total_pages or not page_data:
                break
            page += 1

        return rows

    async def list_workspaces(self) -> List[Dict[str, Any]]:
        """List all workspaces (includeAll=true)."""
        response = await with_retry(
            lambda: self._ensure_client().get_workspaces(),
            max_attempts=3,
        )
        return response.get("data") or []

    async def list_reports(self) -> List[Dict[str, Any]]:
        """List all reports via page-based pagination."""
        reports: List[Dict[str, Any]] = []
        page = 1

        while True:
            response = await with_retry(
                lambda p=page: self._ensure_client().get_reports(
                    page=p,
                    page_size=_DEFAULT_PAGE_SIZE,
                ),
                max_attempts=3,
            )
            page_data: List[Dict[str, Any]] = response.get("data") or []
            reports.extend(page_data)

            total_pages = response.get("totalPages", 1)
            page_number = response.get("pageNumber", page)
            if page_number >= total_pages or not page_data:
                break
            page += 1

        return reports

    async def get_sheet(self, sheet_id: int) -> Dict[str, Any]:
        """Fetch a single sheet by ID with columns and rows."""
        return await with_retry(
            lambda: self._ensure_client().get_sheet(sheet_id=sheet_id),
            max_attempts=3,
        )

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> "SmartsheetConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
