"""Personio HR connector — orchestration only.

All HTTP calls → `client/http_client.py`
All normalization → `helpers/normalizer.py`
All utilities → `helpers/utils.py`

Auth: OAuth2 client-credentials surfacing as `AUTH_TYPE = "api_key"` in the
gateway (user provides a pre-shared `client_id` + `client_secret`). The
Personio v1 bearer rotates on every response — the HTTP client owns rotation;
this module wires `BaseConnector.set_token()` as the persistence sink so a
restart picks up the latest credential.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import PersonioHTTPClient
from exceptions import (
    PersonioAuthError,
    PersonioError,
    PersonioNetworkError,
    PersonioNotFoundError,
    PersonioRateLimitError,
    PersonioServerError,
)
from helpers.normalizer import normalize_employee
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_PERSONIO_BASE = "https://api.personio.de/v1"
# Personio bearers are good for ~24 h; we still rotate on every response.
_TOKEN_TTL_SECONDS = 24 * 60 * 60


class PersonioConnector(BaseConnector):
    """Shielva connector for the Personio HR platform."""

    CONNECTOR_TYPE = "personio"
    CONNECTOR_NAME = "Personio"
    AUTH_TYPE = "api_key"

    # Public required keys — the gateway uses this to validate install_fields.
    # Optional knobs (partner_id, app_id, base_url, rate_limit_per_min) carry
    # safe defaults and are intentionally NOT listed here.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "client_id",
        "client_secret",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    # Read by `health_check()` and any future status mapper.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.base_url: str = self.config.get("base_url", "") or _PERSONIO_BASE
        self.partner_id: str = self.config.get("partner_id", "") or "SHIELVA"
        self.app_id: str = self.config.get("app_id", "") or "shielva-connector"
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = PersonioHTTPClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            base_url=self.base_url,
            partner_id=self.partner_id,
            app_id=self.app_id,
            on_token_rotated=self._persist_rotated_token,
        )

    # ── Token rotation persistence ────────────────────────────────────────

    async def _persist_rotated_token(self, token: str) -> None:
        """Hook invoked by the HTTP client whenever Personio rotates the bearer.

        Routes through `BaseConnector.set_token()` so the platform's connector
        store persists the latest credential — survives restarts and prevents
        the next process from re-running /auth.
        """
        token_info = TokenInfo(
            access_token=token,
            refresh_token=None,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=_TOKEN_TTL_SECONDS),
            token_type="Bearer",
            scopes=[],
            metadata={"rotated": True},
        )
        try:
            await self.set_token(token_info)
        except Exception as exc:  # noqa: BLE001 — persistence must never break
            # the request path. Log + carry on; in-memory token survives.
            logger.warning(
                "personio.persist_token_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )

    # ── BaseConnector lifecycle surface ───────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Does not call Personio — the gateway runs `health_check()` separately
        when it wants reachability confirmation. install() is a fast schema
        check so the UI can show "installed" without paying a network hop.
        """
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        if not client_id or not client_secret:
            logger.warning(
                "personio.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )

        # Push the credentials into the HTTP client so it can authenticate.
        self.client_id = client_id
        self.client_secret = client_secret
        self.http_client.set_credentials(client_id, client_secret)

        await self.save_config(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "base_url": self.base_url,
                "partner_id": self.partner_id,
                "app_id": self.app_id,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("personio.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Connector installed — run authenticate() to connect",
        )

    async def authorize(
        self, auth_code: str = "", state: str = ""
    ) -> TokenInfo:
        """Surface-compat with BaseConnector — delegates to authenticate()."""
        return await self.authenticate()

    async def authenticate(self) -> TokenInfo:
        """Run Personio /auth and persist the resulting bearer.

        Subsequent responses rotate the bearer; the HTTP client's
        `_capture_rotated_token` keeps the cache fresh and fans out to
        `_persist_rotated_token` so the platform's store stays in sync.
        """
        token = await self.http_client.authenticate()
        token_info = TokenInfo(
            access_token=token,
            refresh_token=None,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=_TOKEN_TTL_SECONDS),
            token_type="Bearer",
            scopes=[],
        )
        await self.set_token(token_info)
        logger.info(
            "personio.authenticate.ok",
            connector_id=self.connector_id,
        )
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Probe Personio by listing one employee — proves credentials + reach."""
        try:
            await with_retry(
                lambda: self.http_client.list_employees(limit=1, offset=0),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Personio API reachable",
            )
        except PersonioAuthError as exc:
            # 401 vs 403 — derive from the status code on the exception.
            if exc.status_code == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=str(exc),
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=str(exc),
            )
        except PersonioRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except PersonioNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Personio network error: {exc}",
            )
        except PersonioServerError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except PersonioNotFoundError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except PersonioError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Page through /company/employees and ingest each one as a NormalizedDocument."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        offset = 0
        page_size = 200
        updated_since: Optional[str] = None
        if since and not full:
            updated_since = since.astimezone(timezone.utc).isoformat()

        try:
            while True:
                resp = await with_retry(
                    lambda o=offset: self.http_client.list_employees(
                        limit=page_size,
                        offset=o,
                        updated_since=updated_since,
                    ),
                    max_retries=3,
                )
                data = resp.get("data") or []
                if not data:
                    break

                documents_found += len(data)
                for raw in data:
                    try:
                        doc = normalize_employee(
                            raw, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:  # noqa: BLE001 — per-record isolation
                        logger.error(
                            "personio.sync.record_failed",
                            error=str(exc),
                        )
                        documents_failed += 1

                if len(data) < page_size:
                    break
                offset += page_size

            return SyncResult(
                status=SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} employees",
            )
        except Exception as exc:  # noqa: BLE001 — top-level fault is a sync failure
            logger.error(
                "personio.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API surface — Employees ─────────────────────────────────────

    async def list_employees(
        self,
        limit: int = 100,
        offset: int = 0,
        email: Optional[str] = None,
        updated_since: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /company/employees — paginated employee list."""
        return await with_retry(
            lambda: self.http_client.list_employees(
                limit=limit,
                offset=offset,
                email=email,
                updated_since=updated_since,
            ),
            max_retries=3,
        )

    async def get_employee(self, employee_id: int) -> Dict[str, Any]:
        """GET /company/employees/{id}."""
        return await with_retry(
            lambda: self.http_client.get_employee(employee_id),
            max_retries=3,
        )

    async def create_employee(
        self,
        first_name: str,
        last_name: str,
        email: str,
        hire_date: str,
        department: Optional[str] = None,
        position: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /company/employees."""
        attributes: Dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "hire_date": hire_date,
        }
        if department:
            attributes["department"] = department
        if position:
            attributes["position"] = position
        if extra:
            attributes.update(extra)
        return await with_retry(
            lambda: self.http_client.create_employee(attributes),
            max_retries=3,
        )

    async def update_employee(
        self, employee_id: int, attributes: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PATCH /company/employees/{id}."""
        return await with_retry(
            lambda: self.http_client.update_employee(employee_id, attributes),
            max_retries=3,
        )

    async def list_custom_attributes(self) -> Dict[str, Any]:
        """GET /company/employees/custom-attributes."""
        return await with_retry(
            lambda: self.http_client.list_custom_attributes(),
            max_retries=3,
        )

    async def get_employee_normalized(
        self, employee_id: int
    ) -> NormalizedDocument:
        """Convenience: fetch + normalize a single employee to a document."""
        raw = await self.get_employee(employee_id)
        data = raw.get("data") or raw
        if isinstance(data, list) and data:
            data = data[0]
        return normalize_employee(data, self.connector_id, self.tenant_id)

    # ── Public API surface — Attendances ───────────────────────────────────

    async def list_attendances(
        self,
        employees: Optional[List[int]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /company/attendances."""
        return await with_retry(
            lambda: self.http_client.list_attendances(
                employees=employees,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
                offset=offset,
            ),
            max_retries=3,
        )

    async def create_attendance(
        self,
        employee: int,
        date: str,
        start_time: str,
        end_time: str,
        break_time: int = 0,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /company/attendances."""
        return await with_retry(
            lambda: self.http_client.create_attendance(
                employee=employee,
                date=date,
                start_time=start_time,
                end_time=end_time,
                break_time=break_time,
                comment=comment,
            ),
            max_retries=3,
        )

    async def update_attendance(
        self, attendance_id: int, attributes: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PATCH /company/attendances/{id}."""
        return await with_retry(
            lambda: self.http_client.update_attendance(
                attendance_id, attributes
            ),
            max_retries=3,
        )

    # ── Public API surface — Time-offs ─────────────────────────────────────

    async def list_time_offs(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /company/time-offs."""
        return await with_retry(
            lambda: self.http_client.list_time_offs(
                start_date=start_date,
                end_date=end_date,
                limit=limit,
                offset=offset,
            ),
            max_retries=3,
        )

    async def create_time_off(
        self,
        employee_id: int,
        time_off_type_id: int,
        start_date: str,
        end_date: str,
        half_day_start: bool = False,
        half_day_end: bool = False,
    ) -> Dict[str, Any]:
        """POST /company/time-offs."""
        return await with_retry(
            lambda: self.http_client.create_time_off(
                employee_id=employee_id,
                time_off_type_id=time_off_type_id,
                start_date=start_date,
                end_date=end_date,
                half_day_start=half_day_start,
                half_day_end=half_day_end,
            ),
            max_retries=3,
        )

    async def list_time_off_types(self) -> Dict[str, Any]:
        """GET /company/time-off-types."""
        return await with_retry(
            lambda: self.http_client.list_time_off_types(),
            max_retries=3,
        )

    # ── Public API surface — Documents ─────────────────────────────────────

    async def list_documents(
        self,
        employee_id: int,
        category_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """GET /company/document-categories filtered by employee_id."""
        return await with_retry(
            lambda: self.http_client.list_documents(employee_id, category_id),
            max_retries=3,
        )

    async def upload_document(
        self,
        employee_id: int,
        file_bytes: bytes,
        filename: str,
        category_id: int,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /company/employees/{id}/documents — multipart upload."""
        return await with_retry(
            lambda: self.http_client.upload_document(
                employee_id=employee_id,
                file_bytes=file_bytes,
                filename=filename,
                category_id=category_id,
                title=title,
            ),
            max_retries=2,
        )

    # ── Public API surface — Org structure ─────────────────────────────────

    async def list_departments(self) -> Dict[str, Any]:
        """GET /company/departments."""
        return await with_retry(
            lambda: self.http_client.list_departments(),
            max_retries=3,
        )

    async def list_offices(self) -> Dict[str, Any]:
        """GET /company/offices."""
        return await with_retry(
            lambda: self.http_client.list_offices(),
            max_retries=3,
        )

    async def list_projects(self) -> Dict[str, Any]:
        """GET /company/projects."""
        return await with_retry(
            lambda: self.http_client.list_projects(),
            max_retries=3,
        )

    # ── Public API surface — Recruitment ───────────────────────────────────

    async def list_applications(
        self,
        limit: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /recruiting/applications."""
        return await with_retry(
            lambda: self.http_client.list_applications(
                limit=limit, offset=offset, status=status
            ),
            max_retries=3,
        )

    async def get_applicant(self, applicant_id: int) -> Dict[str, Any]:
        """GET /recruiting/applicants/{id}."""
        return await with_retry(
            lambda: self.http_client.get_applicant(applicant_id),
            max_retries=3,
        )
