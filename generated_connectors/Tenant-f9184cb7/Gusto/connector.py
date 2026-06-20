from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from urllib.parse import urlencode

from client import GustoHTTPClient
from exceptions import (
    GustoAuthError,
    GustoError,
    GustoNetworkError,
)
from helpers import normalize_employee, normalize_payroll, with_retry
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
        def __init__(self, tenant_id: str = "", connector_id: str = "", config: "Dict[str, Any] | None" = None) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

CONNECTOR_TYPE = "gusto"
AUTH_TYPE = "oauth2"
OAUTH_SCOPES = [
    "openid",
    "employees:read",
    "payrolls:read",
    "companies:read",
]
OAUTH_AUTHORIZE_URL = "https://api.gusto.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://api.gusto.com/oauth/token"
EMPLOYEES_PAGE_SIZE = 100


class GustoConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Gusto.

    Syncs employee and payroll data via the Gusto API v1.
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
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)

        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self.http_client: GustoHTTPClient | None = None

    def _make_client(self) -> GustoHTTPClient:
        return GustoHTTPClient()

    def _ensure_client(self) -> GustoHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate OAuth credentials.

        If client_id and client_secret are present but access_token is not yet
        available (OAuth flow not completed), returns HEALTHY/PENDING.
        If credentials are missing entirely, returns OFFLINE/MISSING_CREDENTIALS.
        If an access_token is present, calls /v1/me to verify it.
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
                    "authorize access to Gusto."
                ),
            )

        client = self._make_client()
        try:
            me = await with_retry(client.get_me, self._access_token)
            await client.aclose()
            email: str = me.get("email", "")
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected as {email}" if email else "Connected to Gusto",
            )
        except GustoAuthError as exc:
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

    def authorize(self) -> str:
        """Return the Gusto OAuth2 authorization URL.

        The caller should redirect the user's browser to this URL to initiate
        the OAuth flow.
        """
        params: dict[str, str] = {
            "client_id": self._client_id,
            "response_type": "code",
            "scope": " ".join(OAUTH_SCOPES),
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    async def health_check(self) -> HealthCheckResult:
        """Verify the stored access_token via GET /v1/me."""
        if not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required — complete the OAuth flow",
            )
        client = self._make_client()
        try:
            me = await with_retry(client.get_me, self._access_token)
            await client.aclose()
            email: str = me.get("email", "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected as {email}" if email else "Gusto API is reachable",
            )
        except GustoAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except GustoNetworkError as exc:
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
        """Sync all employees and payrolls across all accessible companies.

        1. Fetches the current user's companies via GET /v1/me
        2. For each company, pages through all employees
        3. For each company, fetches all processed payrolls
        4. Normalizes each record to a ConnectorDocument and optionally
           ingests into the knowledge base identified by kb_id.
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
            companies = await with_retry(client.list_companies, self._access_token)
        except GustoError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Failed to list companies: {exc}",
            )

        for company in companies:
            company_id: str = str(company.get("id", company.get("uuid", "")))
            if not company_id:
                continue

            # Sync employees
            try:
                employee_docs = await self._sync_employees(client, company_id)
                found += len(employee_docs)
                for doc in employee_docs:
                    try:
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except GustoAuthError:
                raise
            except Exception:
                failed += 1

            # Sync payrolls
            try:
                payroll_docs = await self._sync_payrolls(client, company_id)
                found += len(payroll_docs)
                for doc in payroll_docs:
                    try:
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except GustoAuthError:
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

    async def _sync_employees(
        self, client: GustoHTTPClient, company_id: str
    ) -> list[ConnectorDocument]:
        """Paginate all employees for a company and normalize each to a document."""
        documents: list[ConnectorDocument] = []
        page = 1
        while True:
            employees = await with_retry(
                client.list_employees,
                self._access_token,
                company_id,
                page=page,
                per=EMPLOYEES_PAGE_SIZE,
            )
            if not employees:
                break
            for emp in employees:
                documents.append(
                    normalize_employee(emp, company_id, self.connector_id, self.tenant_id)
                )
            if len(employees) < EMPLOYEES_PAGE_SIZE:
                break
            page += 1
        return documents

    async def _sync_payrolls(
        self, client: GustoHTTPClient, company_id: str
    ) -> list[ConnectorDocument]:
        """Fetch all processed payrolls for a company and normalize each."""
        payrolls = await with_retry(
            client.list_payrolls,
            self._access_token,
            company_id,
            processed=True,
        )
        return [
            normalize_payroll(p, company_id, self.connector_id, self.tenant_id)
            for p in payrolls
        ]

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Public API methods ────────────────────────────────────────────────────

    async def get_current_user(self) -> dict[str, Any]:
        """Return the current Gusto user record via GET /v1/me."""
        client = self._ensure_client()
        return await with_retry(client.get_me, self._access_token)

    async def list_companies(self) -> list[dict[str, Any]]:
        """Return all companies accessible to the authorized account."""
        client = self._ensure_client()
        return await with_retry(client.list_companies, self._access_token)

    async def list_employees(
        self, company_id: str, page: int = 1
    ) -> list[dict[str, Any]]:
        """Return one page of employees for the given company."""
        client = self._ensure_client()
        return await with_retry(
            client.list_employees,
            self._access_token,
            company_id,
            page=page,
            per=EMPLOYEES_PAGE_SIZE,
        )

    async def get_employee(
        self, company_id: str, employee_id: str
    ) -> dict[str, Any]:
        """Return a single employee record."""
        client = self._ensure_client()
        return await with_retry(
            client.get_employee,
            self._access_token,
            company_id,
            employee_id,
        )

    async def list_payrolls(
        self, company_id: str, processed: bool = True
    ) -> list[dict[str, Any]]:
        """Return all payrolls for the given company."""
        client = self._ensure_client()
        return await with_retry(
            client.list_payrolls,
            self._access_token,
            company_id,
            processed=processed,
        )

    async def list_departments(self, company_id: str) -> list[dict[str, Any]]:
        """Return all departments for the given company."""
        client = self._ensure_client()
        return await with_retry(client.get_departments, self._access_token, company_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> GustoConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
