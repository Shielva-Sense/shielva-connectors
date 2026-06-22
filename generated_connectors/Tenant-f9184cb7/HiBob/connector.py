"""HiBob (Bob HR) connector — orchestration only.

All HTTP calls -> ``client/http_client.py``
All normalization -> ``helpers/normalizer.py``
All utilities -> ``helpers/utils.py``

Authentication: HTTP Basic with ``{service_user_id}:{service_user_token}`` —
the Service-User credentials issued by HiBob (Bob) at
**Settings -> Integrations -> Service Users**.

Required headers (built inside ``HiBobHTTPClient``):

    Authorization: Basic base64(service_user_id:service_user_token)
    Content-Type:  application/json
    Accept:        application/json
"""
from __future__ import annotations

from datetime import datetime
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

from client.http_client import HiBobHTTPClient
from exceptions import (
    HiBobAuthError,
    HiBobError,
    HiBobNetworkError,
    HiBobNotFound,
    HiBobNotFoundError,
)
from helpers.normalizer import normalize_employee
from helpers.utils import humanize_employee_fields, with_retry

logger = structlog.get_logger(__name__)

_HIBOB_BASE = "https://api.hibob.com/v1"

# Default field projection used by /people/search when the caller doesn't
# supply one. Mirrors HiBob's documented "essentials" projection.
_DEFAULT_PEOPLE_FIELDS: List[str] = [
    "/root/id",
    "/root/firstName",
    "/root/surname",
    "/root/email",
    "/root/displayName",
    "/work/title",
    "/work/department",
    "/work/site",
    "/work/startDate",
]


class HiBobConnector(BaseConnector):
    """Shielva connector for the HiBob (Bob) HR platform."""

    CONNECTOR_TYPE = "hibob"
    CONNECTOR_NAME = "HiBob"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "service_user_id",
        "service_user_token",
    ]

    # OCP — HTTP status -> (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        self.service_user_id: str = self.config.get("service_user_id", "")
        self.service_user_token: str = self.config.get("service_user_token", "")
        self.base_url: str = self.config.get("base_url", "") or _HIBOB_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = HiBobHTTPClient(
            service_user_id=self.service_user_id,
            service_user_token=self.service_user_token,
            base_url=self.base_url,
        )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_credentials(self) -> None:
        if not self.service_user_id or not self.service_user_token:
            raise HiBobAuthError(
                "HiBob service-user credentials missing — install the connector first"
            )
        # Keep the HTTP client in sync if credentials were rotated after init.
        self.http_client.set_credentials(self.service_user_id, self.service_user_token)

    # ── BaseConnector abstract surface ───────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Does NOT call the HiBob API. The gateway separately runs
        ``health_check()`` to verify live credentials.
        """
        service_user_id = self.config.get("service_user_id")
        service_user_token = self.config.get("service_user_token")

        if not service_user_id or not service_user_token:
            logger.warning(
                "hibob.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="service_user_id and service_user_token are required",
            )

        await self.save_config(
            {
                "service_user_id": service_user_id,
                "service_user_token": service_user_token,
                "base_url": self.config.get("base_url", _HIBOB_BASE),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 60),
            }
        )
        self.service_user_id = service_user_id
        self.service_user_token = service_user_token
        self.http_client.set_credentials(service_user_id, service_user_token)

        logger.info("hibob.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="HiBob connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """HiBob uses static Service-User credentials — no OAuth flow.

        Returns a synthetic TokenInfo so the platform's storage contract is
        satisfied. The connector never refreshes a token.
        """
        return TokenInfo(
            access_token=self.service_user_token,
            refresh_token=None,
            expires_at=None,
            token_type="Basic",
            scopes=["hibob:service_user"],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify HiBob API connectivity by listing one employee."""
        try:
            self._ensure_credentials()
            await with_retry(
                lambda: self.http_client.health_check(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="HiBob API reachable",
            )
        except HiBobAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"HiBob auth failed: {exc}",
            )
        except HiBobNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"HiBob network error: {exc}",
            )
        except HiBobError as exc:
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
        """Sync the HiBob employee directory into the Shielva KB.

        Iterates ``/people/search`` results, normalises each employee to
        ``NormalizedDocument`` (tenant-scoped id), and ingests one-by-one via
        ``ingest_document``.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            self._ensure_credentials()
            people_resp = await with_retry(
                lambda: self.http_client.search_people(
                    {"fields": _DEFAULT_PEOPLE_FIELDS, "filters": []}
                ),
                max_retries=3,
            )
            employees = (people_resp or {}).get("employees", []) or []
            documents_found = len(employees)

            for emp in employees:
                try:
                    doc: NormalizedDocument = normalize_employee(
                        emp, self.connector_id, self.tenant_id
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "hibob.sync.employee_failed",
                        employee_id=emp.get("id"),
                        error=str(exc),
                    )
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} HiBob employees",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hibob.sync.failed", error=str(exc), connector_id=self.connector_id
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ───────────────────────────────

    async def list_people(
        self,
        *,
        limit: int = 50,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /people — list employees (simple listing, no projection body)."""
        self._ensure_credentials()
        return await with_retry(
            lambda: self.http_client.list_people(limit=limit, fields=fields),
            max_retries=3,
        )

    async def search_people(
        self,
        filters: Optional[List[Dict[str, Any]]] = None,
        fields: Optional[List[str]] = None,
        *,
        include_humanized: bool = False,
        fields_humanized: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /people/search — bulk-fetch employee records with projection.

        When ``include_humanized`` is True each employee entry gets a
        ``humanized`` projection limited to ``fields_humanized`` (or all keys
        when ``fields_humanized`` is None).
        """
        self._ensure_credentials()
        body = {
            "fields": fields or _DEFAULT_PEOPLE_FIELDS,
            "filters": filters or [],
        }
        resp = await with_retry(
            lambda: self.http_client.search_people(body),
            max_retries=3,
        )
        resp = resp or {}
        if include_humanized:
            for emp in resp.get("employees", []) or []:
                emp["humanized"] = humanize_employee_fields(emp, fields_humanized)
        return resp

    async def get_employee(self, employee_id: str) -> Dict[str, Any]:
        """GET /people/{id}."""
        self._ensure_credentials()
        return await with_retry(
            lambda: self.http_client.get_employee(employee_id),
            max_retries=3,
        )

    async def get_employee_profile(self, employee_id: str) -> Dict[str, Any]:
        """GET /profiles/{id} — humanised full-profile projection."""
        self._ensure_credentials()
        return await with_retry(
            lambda: self.http_client.get_employee_profile(employee_id),
            max_retries=3,
        )

    async def create_employee(
        self,
        first_name: str,
        surname: str,
        email: str,
        work_email: Optional[str] = None,
        start_date: Optional[str] = None,
        company_id: Optional[str] = None,
        site: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /people — create a new employee."""
        self._ensure_credentials()
        body: Dict[str, Any] = {
            "firstName": first_name,
            "surname": surname,
            "email": email,
        }
        if work_email:
            body["workEmail"] = work_email
        work: Dict[str, Any] = {}
        if start_date:
            work["startDate"] = start_date
        if company_id:
            work["companyId"] = company_id
        if site:
            work["site"] = site
        if work:
            body["work"] = work
        return await with_retry(
            lambda: self.http_client.create_employee(body),
            max_retries=2,
        )

    async def update_employee(
        self, employee_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /people/{id} — update employee fields."""
        self._ensure_credentials()
        return await with_retry(
            lambda: self.http_client.update_employee(employee_id, fields),
            max_retries=2,
        )

    async def list_employments(self, employee_id: str) -> Dict[str, Any]:
        """GET /people/{id}/employment."""
        self._ensure_credentials()
        return await with_retry(
            lambda: self.http_client.list_employments(employee_id),
            max_retries=3,
        )

    async def list_time_off_requests(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        policy_type_display_name: Optional[str] = None,
        include_pending: bool = True,
    ) -> Dict[str, Any]:
        """GET /timeoff/requests/changes — list time-off changes in a window."""
        self._ensure_credentials()
        return await with_retry(
            lambda: self.http_client.list_time_off_requests(
                from_date=from_date,
                to_date=to_date,
                policy_type_display_name=policy_type_display_name,
                include_pending=include_pending,
            ),
            max_retries=3,
        )

    async def create_time_off_request(
        self,
        employee_id: str,
        policy_type_display_name: str,
        request_range_type: str,
        start_date: str,
        end_date: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /timeoff/employees/{id}/requests."""
        self._ensure_credentials()
        body: Dict[str, Any] = {
            "policyTypeDisplayName": policy_type_display_name,
            "requestRangeType": request_range_type,
            "startDate": start_date,
        }
        if end_date:
            body["endDate"] = end_date
        if description:
            body["description"] = description
        return await with_retry(
            lambda: self.http_client.create_time_off_request(employee_id, body),
            max_retries=2,
        )

    async def list_payroll(self, employee_id: str) -> Dict[str, Any]:
        """GET /payroll/history/{employee_id}."""
        self._ensure_credentials()
        return await with_retry(
            lambda: self.http_client.list_payroll(employee_id),
            max_retries=3,
        )

    async def list_lifecycle_changes(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /people/lifecycle/changes."""
        self._ensure_credentials()
        return await with_retry(
            lambda: self.http_client.list_lifecycle_changes(
                from_date=from_date, to_date=to_date
            ),
            max_retries=3,
        )

    async def list_departments(self) -> Dict[str, Any]:
        """GET /company/named-lists/department."""
        self._ensure_credentials()
        return await with_retry(
            lambda: self.http_client.list_departments(),
            max_retries=3,
        )

    async def list_sites(self) -> Dict[str, Any]:
        """GET /company/named-lists/site."""
        self._ensure_credentials()
        return await with_retry(
            lambda: self.http_client.list_sites(),
            max_retries=3,
        )
