from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import GoogleSheetsHTTPClient
from exceptions import (
    GoogleSheetsAuthError,
    GoogleSheetsError,
    GoogleSheetsNetworkError,
)
from helpers import normalize_sheet_rows, normalize_spreadsheet, with_retry
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
    from shared.base_connector import BaseConnector
    _BASE = BaseConnector
except ImportError:
    _BASE = object  # standalone / test mode

CONNECTOR_TYPE = "google_sheets"
AUTH_TYPE = "oauth2"
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
SYNC_PAGE_SIZE = 100


class GoogleSheetsConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Google Sheets.

    Syncs spreadsheet row data via the Google Sheets API v4 and Drive API v3.
    Uses OAuth 2.0 — the caller must supply a valid access_token in config.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(
                tenant_id=tenant_id, connector_id=connector_id, config=_config
            )
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self.http_client: GoogleSheetsHTTPClient | None = None

    def _make_client(self) -> GoogleSheetsHTTPClient:
        return GoogleSheetsHTTPClient()

    def _ensure_client(self) -> GoogleSheetsHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate OAuth credentials.

        If client_id and client_secret are present but access_token is not yet
        available (OAuth flow not completed), returns HEALTHY/PENDING.
        If credentials are missing entirely, returns OFFLINE/MISSING_CREDENTIALS.
        If an access_token is present, calls userinfo to verify it.
        """
        if not self._client_id or not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )

        if not self._access_token:
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.PENDING,
                connector_id=self.connector_id,
                message=(
                    "OAuth credentials accepted. Complete the OAuth flow to "
                    "authorize access to Google Sheets."
                ),
            )

        client = self._make_client()
        try:
            userinfo = await with_retry(client.get_userinfo, self._access_token)
            await client.aclose()
            email: str = userinfo.get("email", "")
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected as {email}" if email else "Connected to Google Sheets",
            )
        except GoogleSheetsAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"OAuth token rejected: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Verify the stored access_token via GET /oauth2/v2/userinfo."""
        if not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required — complete the OAuth flow",
            )
        client = self._make_client()
        try:
            userinfo = await with_retry(client.get_userinfo, self._access_token)
            await client.aclose()
            email: str = userinfo.get("email", "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected as {email}" if email else "Google Sheets API is reachable",
            )
        except GoogleSheetsAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except GoogleSheetsNetworkError as exc:
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

    async def sync(
        self,
        full: bool = False,  # noqa: ARG002 — reserved for incremental future
        since: datetime | None = None,  # noqa: ARG002 — reserved
        kb_id: str = "",
    ) -> SyncResult:
        """Sync all accessible spreadsheets — all sheets, all rows.

        Lists every Google Sheets file accessible to the OAuth token, fetches
        all sheet data, normalizes each row to a ConnectorDocument, and
        optionally ingests into the knowledge base identified by kb_id.
        """
        if not self._access_token:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="access_token is required — complete the OAuth flow",
            )

        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0

        try:
            spreadsheet_files = await self._list_all_spreadsheet_files(client)
        except GoogleSheetsError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Failed to list spreadsheets: {exc}",
            )

        for file_meta in spreadsheet_files:
            spreadsheet_id: str = file_meta.get("id", "")
            if not spreadsheet_id:
                continue
            try:
                docs = await self._sync_spreadsheet(client, spreadsheet_id)
                found += len(docs)
                for doc in docs:
                    try:
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except GoogleSheetsAuthError:
                raise
            except Exception:
                failed += 1

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _list_all_spreadsheet_files(
        self, client: GoogleSheetsHTTPClient
    ) -> list[dict[str, Any]]:
        """Paginate through all Drive files of type google-apps.spreadsheet."""
        files: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            page = await with_retry(
                client.list_spreadsheets,
                self._access_token,
                page_token=page_token,
                page_size=SYNC_PAGE_SIZE,
            )
            files.extend(page.get("files", []))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return files

    async def _sync_spreadsheet(
        self, client: GoogleSheetsHTTPClient, spreadsheet_id: str
    ) -> list[ConnectorDocument]:
        """Fetch all sheets for a spreadsheet and return all row documents."""
        spreadsheet = await with_retry(
            client.get_spreadsheet, self._access_token, spreadsheet_id
        )
        documents: list[ConnectorDocument] = []

        # Spreadsheet-level document
        documents.append(
            normalize_spreadsheet(spreadsheet, self.connector_id, self._tenant_id)
        )

        sheets: list[dict[str, Any]] = spreadsheet.get("sheets", [])
        for sheet in sheets:
            sheet_title: str = sheet.get("properties", {}).get("title", "")
            if not sheet_title:
                continue
            try:
                range_ = f"'{sheet_title}'!A:Z"
                values_response = await with_retry(
                    client.get_values,
                    self._access_token,
                    spreadsheet_id,
                    range_,
                )
                rows: list[list[str]] = values_response.get("values", [])
                if not rows:
                    continue
                headers = [str(h) for h in rows[0]]
                data_rows = [[str(cell) for cell in row] for row in rows[1:]]
                row_docs = normalize_sheet_rows(
                    spreadsheet_id=spreadsheet_id,
                    sheet_title=sheet_title,
                    headers=headers,
                    rows=data_rows,
                    connector_id=self.connector_id,
                    tenant_id=self._tenant_id,
                )
                documents.extend(row_docs)
            except GoogleSheetsAuthError:
                raise
            except Exception:
                pass

        return documents

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Public API methods ────────────────────────────────────────────────────

    async def list_spreadsheets(self, page_size: int = 100) -> list[dict[str, Any]]:
        """List all Google Sheets files accessible via the OAuth token (paginated)."""
        client = self._ensure_client()
        return await self._list_all_spreadsheet_files(client)

    async def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]:
        """Return spreadsheet metadata + sheet names."""
        client = self._ensure_client()
        return await with_retry(
            client.get_spreadsheet, self._access_token, spreadsheet_id
        )

    async def get_sheet_values(
        self, spreadsheet_id: str, sheet_title: str
    ) -> dict[str, Any]:
        """Return all cell values for a named sheet (columns A through Z)."""
        client = self._ensure_client()
        range_ = f"'{sheet_title}'!A:Z"
        return await with_retry(
            client.get_values, self._access_token, spreadsheet_id, range_
        )

    async def get_spreadsheet_data(
        self, spreadsheet_id: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Return full data for all sheets in a spreadsheet.

        Returns a dict mapping sheet title → list of row dicts
        (header→value mappings).
        """
        client = self._ensure_client()
        spreadsheet = await with_retry(
            client.get_spreadsheet, self._access_token, spreadsheet_id
        )
        sheets: list[dict[str, Any]] = spreadsheet.get("sheets", [])
        result: dict[str, list[dict[str, Any]]] = {}

        for sheet in sheets:
            sheet_title: str = sheet.get("properties", {}).get("title", "")
            if not sheet_title:
                continue
            try:
                range_ = f"'{sheet_title}'!A:Z"
                values_response = await with_retry(
                    client.get_values,
                    self._access_token,
                    spreadsheet_id,
                    range_,
                )
                rows: list[list[str]] = values_response.get("values", [])
                if not rows:
                    result[sheet_title] = []
                    continue
                headers = [str(h) for h in rows[0]]
                sheet_rows: list[dict[str, Any]] = []
                for row in rows[1:]:
                    row_dict: dict[str, Any] = {}
                    for col_idx, header in enumerate(headers):
                        row_dict[header] = row[col_idx] if col_idx < len(row) else ""
                    sheet_rows.append(row_dict)
                result[sheet_title] = sheet_rows
            except Exception:
                result[sheet_title] = []

        return result

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> GoogleSheetsConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
