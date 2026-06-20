from __future__ import annotations

from typing import Any

from client import AcuityHTTPClient
from exceptions import AcuityAuthError, AcuityError, AcuityNetworkError
from helpers import (
    normalize_appointment,
    normalize_appointment_type,
    normalize_client,
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
            self.config = config or {}

SYNC_PAGE_SIZE: int = 25
CONNECTOR_TYPE: str = "acuity_scheduling"
AUTH_TYPE: str = "api_key"


class AcuitySchedulingConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Acuity Scheduling.

    Syncs appointments, clients, and appointment types from the Acuity
    Scheduling REST API v1 using HTTP BasicAuth (user_id + api_key).
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
        try:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        except TypeError:
            self.config = _config
            self.connector_id = connector_id
            self.tenant_id = tenant_id

        self._user_id: str = _config.get("user_id", "")
        self._api_key: str = _config.get("api_key", "")
        self._http_client: AcuityHTTPClient | None = None

    def _make_client(self) -> AcuityHTTPClient:
        return AcuityHTTPClient()

    def _ensure_client(self) -> AcuityHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._user_id:
            missing.append("user_id")
        if not self._api_key:
            missing.append("api_key")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate user_id + api_key by calling GET /api/v1/me."""
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
                client.get_me,
                self._user_id,
                self._api_key,
            )
            business_name: str = data.get("name", "") or data.get("email", "")
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Acuity Scheduling as {business_name}",
            )
        except AcuityAuthError as exc:
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
        """Ping GET /api/v1/me and return current health status."""
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
                client.get_me,
                self._user_id,
                self._api_key,
            )
            business_name: str = (
                data.get("name", "") or data.get("email", "") or "unknown"
            )
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Acuity Scheduling API reachable. Account: {business_name}",
            )
        except AcuityAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except AcuityNetworkError as exc:
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
        min_date: str | None = None,
        max_date: str | None = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync appointments, clients, and appointment types into the knowledge base.

        Fetches all three resource types and normalizes each to a ConnectorDocument.
        Returns COMPLETED if zero failures, PARTIAL if some items failed, FAILED if
        a critical endpoint is unreachable.
        """
        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0

        # Sync appointments (paginated)
        page = 1
        while True:
            try:
                appointments: list[dict[str, Any]] = await with_retry(
                    client.get_appointments,
                    self._user_id,
                    self._api_key,
                    page=page,
                    max=SYNC_PAGE_SIZE,
                    min_date=min_date,
                    max_date=max_date,
                )
            except AcuityError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            if not appointments:
                break

            found += len(appointments)
            for appt in appointments:
                try:
                    doc = normalize_appointment(appt, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            if len(appointments) < SYNC_PAGE_SIZE:
                break
            page += 1

        # Sync clients (paginated)
        client_page = 1
        while True:
            try:
                clients: list[dict[str, Any]] = await with_retry(
                    client.get_clients,
                    self._user_id,
                    self._api_key,
                    page=client_page,
                )
            except AcuityError:
                # Non-fatal — continue with appointment types
                break

            if not clients:
                break

            found += len(clients)
            for c in clients:
                try:
                    doc = normalize_client(c, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            if len(clients) < SYNC_PAGE_SIZE:
                break
            client_page += 1

        # Sync appointment types (not paginated)
        try:
            appt_types: list[dict[str, Any]] = await with_retry(
                client.get_appointment_types,
                self._user_id,
                self._api_key,
            )
            found += len(appt_types)
            for at in appt_types:
                try:
                    doc = normalize_appointment_type(at, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except AcuityError:
            pass  # Non-fatal — appointment types are supplementary

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

    # ── Public API methods ────────────────────────────────────────────────────

    async def list_appointments(
        self,
        min_date: str | None = None,
        max_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the first page of appointments, optionally filtered by date range."""
        client = self._ensure_client()
        return await with_retry(
            client.get_appointments,
            self._user_id,
            self._api_key,
            page=1,
            max=SYNC_PAGE_SIZE,
            min_date=min_date,
            max_date=max_date,
        )

    async def list_clients(self) -> list[dict[str, Any]]:
        """Return the first page of clients."""
        client = self._ensure_client()
        return await with_retry(
            client.get_clients,
            self._user_id,
            self._api_key,
            page=1,
        )

    async def list_appointment_types(self) -> list[dict[str, Any]]:
        """Return all appointment types."""
        client = self._ensure_client()
        return await with_retry(
            client.get_appointment_types,
            self._user_id,
            self._api_key,
        )

    async def list_calendars(self) -> list[dict[str, Any]]:
        """Return all calendars."""
        client = self._ensure_client()
        return await with_retry(
            client.get_calendars,
            self._user_id,
            self._api_key,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> AcuitySchedulingConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
