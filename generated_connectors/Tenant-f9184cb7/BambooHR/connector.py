from __future__ import annotations

from typing import Any, Dict

from client import BambooHRHTTPClient
from exceptions import BambooHRAuthError, BambooHRError, BambooHRNetworkError
from helpers import normalize_employee, with_retry
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

CONNECTOR_TYPE: str = "bamboohr"
AUTH_TYPE: str = "api_key"


class BambooHRConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for BambooHR.

    Syncs employee directory records and time-off requests from the
    BambooHR REST API v1, using HTTP Basic Auth (api_key / "x").
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
        # Convenience keyword args for standalone / test usage
        company_domain: str = "",
        api_key: str = "",
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

        self._company_domain: str = _config.get("company_domain", "") or company_domain
        self._api_key: str = _config.get("api_key", "") or api_key
        self._http_client: BambooHRHTTPClient | None = None

    def _make_client(self) -> BambooHRHTTPClient:
        return BambooHRHTTPClient()

    def _ensure_client(self) -> BambooHRHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._company_domain:
            missing.append("company_domain")
        if not self._api_key:
            missing.append("api_key")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling GET /employees/directory."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_employee_directory,
                self._company_domain,
                self._api_key,
            )
            company_name: str = (
                data.get("meta", {}).get("companyName", "")
                or data.get("companyName", "")
                or self._company_domain
            )
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to BambooHR: {company_name}",
            )
        except BambooHRAuthError as exc:
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
        """Ping GET /employees/directory and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_employee_directory,
                self._company_domain,
                self._api_key,
            )
            company_name: str = (
                data.get("meta", {}).get("companyName", "")
                or data.get("companyName", "")
                or self._company_domain
            )
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"BambooHR API reachable. Company: {company_name}",
            )
        except BambooHRAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except BambooHRNetworkError as exc:
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

    async def sync(
        self,
        full: bool = False,
        since: object = None,
        kb_id: str = "",
    ) -> SyncResult:
        """Sync all BambooHR employees and time-off requests into the knowledge base.

        Fetches the full employee directory (BambooHR does not support
        server-side incremental filtering — all updates are determined
        client-side via the returned data).
        """
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        # ── Employee directory ────────────────────────────────────────────────
        try:
            dir_data = await with_retry(
                client.get_employee_directory,
                self._company_domain,
                self._api_key,
            )
        except BambooHRError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        employees: list[dict[str, Any]] = dir_data.get("employees", [])
        found += len(employees)

        for emp in employees:
            try:
                doc = normalize_employee(
                    emp,
                    self.connector_id,
                    self._tenant_id,
                    self._company_domain,
                )
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # ── Time-off requests ─────────────────────────────────────────────────
        # Sync current-year time-off as supplementary documents
        try:
            from datetime import date

            today = date.today()
            year_start = f"{today.year}-01-01"
            year_end = f"{today.year}-12-31"
            time_off_items = await with_retry(
                client.list_time_off_requests,
                self._company_domain,
                self._api_key,
                year_start,
                year_end,
            )
            found += len(time_off_items)
            for item in time_off_items:
                try:
                    doc = _normalize_time_off(
                        item,
                        self.connector_id,
                        self._tenant_id,
                        self._company_domain,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Exception:
            # Time-off sync failure is non-fatal — employee data still synced
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Employee Directory ────────────────────────────────────────────────────

    async def get_employee_directory(self) -> dict[str, Any]:
        """Return the full BambooHR employee directory."""
        client = self._ensure_client()
        return await with_retry(
            client.get_employee_directory,
            self._company_domain,
            self._api_key,
        )

    async def get_employee(
        self,
        employee_id: str | int,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return a single employee's record with optional field selection."""
        client = self._ensure_client()
        return await with_retry(
            client.get_employee,
            self._company_domain,
            self._api_key,
            employee_id,
            fields=fields,
        )

    # ── Time-Off ──────────────────────────────────────────────────────────────

    async def list_time_off_requests(
        self,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """Return time-off requests within a date range (YYYY-MM-DD)."""
        client = self._ensure_client()
        return await with_retry(
            client.list_time_off_requests,
            self._company_domain,
            self._api_key,
            start_date,
            end_date,
        )

    # ── Reports ───────────────────────────────────────────────────────────────

    async def list_custom_reports(
        self, report_id: str | int
    ) -> dict[str, Any]:
        """Run a saved custom report and return the results."""
        client = self._ensure_client()
        return await with_retry(
            client.list_custom_reports,
            self._company_domain,
            self._api_key,
            report_id,
        )

    # ── Company ───────────────────────────────────────────────────────────────

    async def get_company_info(self) -> dict[str, Any]:
        """Return company metadata from GET /company/info."""
        client = self._ensure_client()
        return await with_retry(
            client.get_company_info,
            self._company_domain,
            self._api_key,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> BambooHRConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _normalize_time_off(
    item: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    company_domain: str,
) -> ConnectorDocument:
    """Convert a BambooHR time-off request into a ConnectorDocument."""
    import hashlib

    request_id: str = str(item.get("id", ""))
    employee_id: str = str(item.get("employeeId", "") or item.get("employee_id", ""))
    employee_name: str = (
        item.get("name", "")
        or item.get("employeeName", "")
        or f"Employee {employee_id}"
    )
    time_off_type: str = item.get("type", {}).get("name", "") if isinstance(item.get("type"), dict) else str(item.get("type", ""))
    status: str = item.get("status", {}).get("status", "") if isinstance(item.get("status"), dict) else str(item.get("status", ""))
    start: str = item.get("start", "")
    end: str = item.get("end", "")
    amount: str = str(item.get("amount", {}).get("amount", "") if isinstance(item.get("amount"), dict) else item.get("amount", ""))
    notes: str = item.get("notes", {}).get("employee", {}).get("note", "") if isinstance(item.get("notes"), dict) else ""

    content_parts: list[str] = [
        f"Employee: {employee_name}",
        f"Time-Off Type: {time_off_type}" if time_off_type else "",
        f"Status: {status}" if status else "",
        f"Start: {start}" if start else "",
        f"End: {end}" if end else "",
        f"Amount: {amount}" if amount else "",
        f"Notes: {notes}" if notes else "",
    ]
    content = "\n".join(p for p in content_parts if p)

    source_id = hashlib.sha256(f"timeoff:{request_id}".encode()).hexdigest()[:16]
    title = f"Time-Off Request #{request_id}: {employee_name} ({start} – {end})"
    source_url = f"https://{company_domain}.bamboohr.com/time_off/request/{request_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "request_id": request_id,
            "employee_id": employee_id,
            "employee_name": employee_name,
            "time_off_type": time_off_type,
            "status": status,
            "start": start,
            "end": end,
            "amount": amount,
        },
    )
