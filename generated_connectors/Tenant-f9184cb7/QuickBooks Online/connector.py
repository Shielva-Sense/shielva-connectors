from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List

from client.http_client import (
    INTUIT_AUTH_URL,
    INTUIT_TOKEN_URL,
    QBO_SCOPE,
    QuickBooksHTTPClient,
)
from exceptions import (
    QuickBooksAuthError,
    QuickBooksError,
    QuickBooksNetworkError,
)
from helpers.utils import (
    normalize_account,
    normalize_customer,
    normalize_invoice,
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
    from shared.base_connector import BaseConnector

    _BASE = BaseConnector
except ImportError:
    _BASE = object  # standalone / test mode

CONNECTOR_TYPE = "quickbooks"
SYNC_MAX_RESULTS = 100
CIRCUIT_BREAKER_THRESHOLD = 5


class QuickBooksConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for QuickBooks Online.

    Provides OAuth2 authorization, health checks, full sync (invoices,
    customers, accounts), and direct access to QBO API resources.
    """

    CONNECTOR_TYPE: str = "quickbooks"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(  # type: ignore[call-arg]
                tenant_id=tenant_id,
                connector_id=connector_id,
                config=_config,
            )
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        # OAuth2 credentials from install_fields
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")

        # Set after OAuth callback
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")
        self._realm_id: str = _config.get("realm_id", "")

        self.http_client: QuickBooksHTTPClient | None = None

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _make_client(self) -> QuickBooksHTTPClient:
        return QuickBooksHTTPClient(
            access_token=self._access_token,
            realm_id=self._realm_id,
        )

    def _ensure_client(self) -> QuickBooksHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """
        Validate the client_id and client_secret are present.

        For OAuth2 connectors, full validation happens after the OAuth flow
        completes. This step confirms credentials are configured and returns
        a PENDING_OAUTH status if the access token is not yet available.
        """
        if not self._client_id or not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )

        # If we already have an access token + realm_id, verify them now
        if self._access_token and self._realm_id:
            return await self._verify_token()

        # Credentials present but OAuth not yet completed
        return InstallResult(
            health=ConnectorHealth.DEGRADED,
            auth_status=AuthStatus.PENDING_OAUTH,
            connector_id=self.connector_id,
            message=(
                "Credentials saved. Complete OAuth authorization via authorize() "
                "to finish connecting."
            ),
        )

    async def _verify_token(self) -> InstallResult:
        """Verify the stored access token by hitting the company info endpoint."""
        client = self._make_client()
        try:
            data = await with_retry(client.get_companyinfo)
            await client.aclose()
            company = data.get("CompanyInfo", {})
            company_name = company.get("CompanyName", "")
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or self._realm_id,
                message=f"Connected to QuickBooks company: {company_name}",
            )
        except QuickBooksAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid or expired access token: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    def authorize(self, state: str = "") -> str:
        """
        Build and return the Intuit OAuth2 authorization URL.

        The user should be redirected to this URL to grant access.
        After consent, Intuit redirects to redirect_uri with `code` and `realmId`.
        """
        params: dict[str, str] = {
            "client_id": self._client_id,
            "response_type": "code",
            "scope": QBO_SCOPE,
            "redirect_uri": self._redirect_uri or "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl",
            "state": state or "shielva_qbo",
        }
        return f"{INTUIT_AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str, realm_id: str) -> dict[str, Any]:
        """
        Exchange an authorization code for access + refresh tokens.

        Stores realm_id, access_token, and refresh_token in self.config.
        Returns the raw token response dict.
        """
        temp_client = QuickBooksHTTPClient(access_token="", realm_id="")
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri or "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl",
        }
        token_resp = await temp_client.post_form_data(
            INTUIT_TOKEN_URL,
            data=data,
            basic_auth=(self._client_id, self._client_secret),
        )
        await temp_client.aclose()

        self._access_token = token_resp.get("access_token", "")
        self._refresh_token = token_resp.get("refresh_token", "")
        self._realm_id = realm_id
        self.config["access_token"] = self._access_token
        self.config["refresh_token"] = self._refresh_token
        self.config["realm_id"] = self._realm_id
        self.http_client = self._make_client()
        return token_resp

    async def refresh_access_token(self) -> dict[str, Any]:
        """Use the stored refresh token to obtain a new access token."""
        if not self._refresh_token:
            raise QuickBooksAuthError("No refresh token available — re-authorize")
        temp_client = QuickBooksHTTPClient(access_token="", realm_id="")
        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        token_resp = await temp_client.post_form_data(
            INTUIT_TOKEN_URL,
            data=data,
            basic_auth=(self._client_id, self._client_secret),
        )
        await temp_client.aclose()

        self._access_token = token_resp.get("access_token", self._access_token)
        new_refresh = token_resp.get("refresh_token", "")
        if new_refresh:
            self._refresh_token = new_refresh
        self.config["access_token"] = self._access_token
        self.config["refresh_token"] = self._refresh_token
        self.http_client = self._make_client()
        return token_resp

    async def health_check(self) -> HealthCheckResult:
        """Verify connectivity by calling GET /companyinfo/{realm_id}."""
        if not self._access_token or not self._realm_id:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token and realm_id are required — complete OAuth flow first",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_companyinfo)
            await client.aclose()
            company = data.get("CompanyInfo", {})
            company_name = company.get("CompanyName", self._realm_id)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"QuickBooks Online API reachable — {company_name}",
            )
        except QuickBooksAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except QuickBooksNetworkError as exc:
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

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = True,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync QBO invoices, customers, and accounts into the knowledge base.

        Fetches up to SYNC_MAX_RESULTS of each entity type.
        """
        if not self._access_token or not self._realm_id:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="No access token or realm_id — complete OAuth flow first",
            )

        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0

        # Invoices
        try:
            inv_resp = await with_retry(client.list_invoices, SYNC_MAX_RESULTS)
            invoices: List[dict[str, Any]] = (
                inv_resp.get("QueryResponse", {}).get("Invoice", [])
            )
            found += len(invoices)
            for inv in invoices:
                try:
                    doc = normalize_invoice(inv, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except QuickBooksError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Invoice sync failed: {exc}",
            )

        # Customers
        try:
            cust_resp = await with_retry(client.list_customers, SYNC_MAX_RESULTS)
            customers: List[dict[str, Any]] = (
                cust_resp.get("QueryResponse", {}).get("Customer", [])
            )
            found += len(customers)
            for cust in customers:
                try:
                    doc = normalize_customer(cust, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except QuickBooksError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Customer sync failed: {exc}",
            )

        # Accounts
        try:
            acct_resp = await with_retry(client.list_accounts, SYNC_MAX_RESULTS)
            accounts: List[dict[str, Any]] = (
                acct_resp.get("QueryResponse", {}).get("Account", [])
            )
            found += len(accounts)
            for acct in accounts:
                try:
                    doc = normalize_account(acct, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except QuickBooksError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Account sync failed: {exc}",
            )

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

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(self, max_results: int = 100) -> dict[str, Any]:
        """SELECT * FROM Customer MAXRESULTS {max_results}."""
        client = self._ensure_client()
        return await with_retry(client.list_customers, max_results)

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        """GET /customer/{customer_id}."""
        client = self._ensure_client()
        return await with_retry(client.get_customer, customer_id)

    # ── Invoices ─────────────────────────────────────────────────────────────

    async def list_invoices(self, max_results: int = 100) -> dict[str, Any]:
        """SELECT * FROM Invoice MAXRESULTS {max_results}."""
        client = self._ensure_client()
        return await with_retry(client.list_invoices, max_results)

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        """GET /invoice/{invoice_id}."""
        client = self._ensure_client()
        return await with_retry(client.get_invoice, invoice_id)

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def list_accounts(self, max_results: int = 100) -> dict[str, Any]:
        """SELECT * FROM Account MAXRESULTS {max_results}."""
        client = self._ensure_client()
        return await with_retry(client.list_accounts, max_results)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> QuickBooksConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
