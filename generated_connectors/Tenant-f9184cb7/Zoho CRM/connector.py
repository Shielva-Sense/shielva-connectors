from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlencode

from client import ZohoCRMHTTPClient
from exceptions import ZohoCRMAuthError, ZohoCRMError, ZohoCRMNetworkError
from helpers import CircuitBreaker, normalize_record, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

from shared.base_connector import BaseConnector


CONNECTOR_TYPE: str = "zoho_crm"
SYNC_PAGE_SIZE: int = 200
CIRCUIT_BREAKER_THRESHOLD: int = 5
OAUTH_SCOPE: str = (
    "ZohoCRM.modules.contacts.READ,"
    "ZohoCRM.modules.leads.READ,"
    "ZohoCRM.modules.accounts.READ,"
    "ZohoCRM.modules.deals.READ,"
    "ZohoCRM.settings.fields.READ"
)

# Modules fetched during full sync
SYNC_MODULES: list[str] = ["Leads", "Contacts", "Accounts", "Deals"]


class ZohoCRMConnector(BaseConnector):
    """
    Shielva connector for Zoho CRM REST API v2.

    Provides OAuth2 authorization URL generation, credential validation,
    health checks, full/incremental sync of Leads, Contacts, Accounts, and Deals,
    and direct access to any Zoho CRM module.
    """

    CONNECTOR_TYPE: str = "zoho_crm"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)

        # Zoho-specific attrs; accept both "dc" and legacy "data_center" key
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        # "dc" is the canonical key; fall back to "data_center" for backwards compat
        dc_raw: str = _config.get("dc", _config.get("data_center", "com")) or "com"
        self._data_center: str = dc_raw.strip().lower()
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")

        self.http_client: ZohoCRMHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    # ── DC helpers ────────────────────────────────────────────────────────────

    def _get_dc(self) -> str:
        """Return the configured data-center suffix (e.g. 'com', 'eu', 'in')."""
        return self._data_center

    def _accounts_url(self) -> str:
        """Return the OAuth/accounts base URL for the current data center."""
        return f"https://accounts.zoho.{self._get_dc()}"

    def _api_url(self) -> str:
        """Return the REST API base URL for the current data center."""
        return f"https://www.zohoapis.{self._get_dc()}/crm/v2"

    # ── Client factory ────────────────────────────────────────────────────────

    def _make_client(self) -> ZohoCRMHTTPClient:
        return ZohoCRMHTTPClient(
            access_token=self._access_token,
            data_center=self._data_center,
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=self._redirect_uri,
        )

    def _ensure_client(self) -> ZohoCRMHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def authorize(self) -> str:
        """Return the Zoho OAuth2 authorization URL for the configured data center.

        Redirects the user to Zoho's consent screen. After approval Zoho
        redirects to redirect_uri with a ``code`` param that must be exchanged
        for tokens via the Zoho token endpoint.
        """
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
            "scope": OAUTH_SCOPE,
            "access_type": "offline",
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        auth_url = f"{self._accounts_url()}/oauth/v2/auth"
        return f"{auth_url}?{urlencode(params)}"

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate OAuth credentials by fetching org info.

        Requires ``client_id`` and ``client_secret`` (used for token refresh by
        the platform) and a valid ``access_token`` to call the API.
        """
        if not self._client_id or not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )
        if not self._access_token:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required — complete OAuth authorization first",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_org)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Zoho CRM successfully",
            )
        except ZohoCRMAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
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
        """Ping Zoho CRM via GET /org; return HEALTHY/DEGRADED/OFFLINE."""
        if not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_org)
            await client.aclose()
            self._circuit_breaker.on_success()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Zoho CRM API is reachable",
            )
        except ZohoCRMAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ZohoCRMNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = (
                ConnectorHealth.DEGRADED
                if not self._circuit_breaker.is_open
                else ConnectorHealth.OFFLINE
            )
            return HealthCheckResult(
                health=health,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(self, kb_id: str = "", **kwargs: Any) -> SyncResult:
        """Sync Zoho CRM Leads, Contacts, Accounts, and Deals into the knowledge base.

        Fetches all pages for each module using Zoho REST API v2
        pagination (page / per_page / more_records).
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        for module in SYNC_MODULES:
            page = 1
            while True:
                try:
                    response = await with_retry(
                        self.http_client.list_records,
                        module,
                        page,
                        SYNC_PAGE_SIZE,
                    )
                except ZohoCRMError:
                    failed += 1
                    break

                records: list[dict[str, Any]] = response.get("data", [])
                info: dict[str, Any] = response.get("info", {})
                found += len(records)

                for record in records:
                    try:
                        doc: ConnectorDocument = normalize_record(
                            module,
                            record,
                            self.connector_id,
                            self.tenant_id,
                            self._data_center,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                more_records: bool = info.get("more_records", False)
                if not more_records:
                    break
                page += 1

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

    # ── Typed list helpers (spec-required) ────────────────────────────────────

    async def list_contacts(self, page: int = 1, per_page: int = 200) -> list[dict[str, Any]]:
        """GET /Contacts?page={page}&per_page={per_page} → list of contact records."""
        client = self._ensure_client()
        response = await with_retry(client.get_contacts, page, per_page)
        return response.get("data", [])

    async def list_leads(self, page: int = 1, per_page: int = 200) -> list[dict[str, Any]]:
        """GET /Leads?page={page}&per_page={per_page} → list of lead records."""
        client = self._ensure_client()
        response = await with_retry(client.get_leads, page, per_page)
        return response.get("data", [])

    async def list_accounts(self, page: int = 1, per_page: int = 200) -> list[dict[str, Any]]:
        """GET /Accounts?page={page}&per_page={per_page} → list of account records."""
        client = self._ensure_client()
        response = await with_retry(client.get_accounts, page, per_page)
        return response.get("data", [])

    async def list_deals(self, page: int = 1, per_page: int = 200) -> list[dict[str, Any]]:
        """GET /Deals?page={page}&per_page={per_page} → list of deal records."""
        client = self._ensure_client()
        response = await with_retry(client.get_deals, page, per_page)
        return response.get("data", [])

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        """GET /Contacts/{contact_id} — fetch a single contact record."""
        client = self._ensure_client()
        return await with_retry(client.get_contact, contact_id)

    # ── Generic module records ────────────────────────────────────────────────

    async def list_records(
        self,
        module: str,
        page: int = 1,
        per_page: int = 200,
    ) -> dict[str, Any]:
        """GET /{module}?page={page}&per_page={per_page} — any module."""
        client = self._ensure_client()
        return await with_retry(client.list_records, module, page, per_page)

    async def get_record(self, module: str, record_id: str) -> dict[str, Any]:
        """GET /{module}/{record_id} — fetch a single record."""
        client = self._ensure_client()
        return await with_retry(client.get_record, module, record_id)

    async def search_records(self, module: str, criteria: str) -> dict[str, Any]:
        """GET /{module}/search?criteria={criteria} — search records by criteria string."""
        client = self._ensure_client()
        return await with_retry(client.search_records, module, criteria)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> ZohoCRMConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
