from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict
from urllib.parse import urlencode

import httpx

from client.http_client import XeroHTTPClient
from exceptions import XeroAuthError, XeroError, XeroNetworkError
from helpers.utils import normalize_account, normalize_contact, normalize_invoice, with_retry
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

XERO_AUTH_URL = "https://login.xero.com/identity/connect/authorize"
XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_CONNECTIONS_URL = "https://api.xero.com/connections"
XERO_BASE_URL = "https://api.xero.com/api.xro/2.0"

XERO_SCOPES = "accounting.transactions accounting.contacts offline_access"
SYNC_PAGE_SIZE = 100


class XeroConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Xero Accounting.

    Provides OAuth2 PKCE authorization, health checks, full/incremental sync,
    and direct access to Xero Accounting API v2 resources (invoices, contacts,
    accounts).
    """

    CONNECTOR_TYPE: str = "xero"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id
        # Xero-specific attrs
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self._xero_tenant_id: str = _config.get("xero_tenant_id", "")
        self.http_client: XeroHTTPClient | None = None

    def _make_client(self) -> XeroHTTPClient:
        return XeroHTTPClient(
            access_token=self._access_token,
            xero_tenant_id=self._xero_tenant_id,
        )

    def _ensure_client(self) -> XeroHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & install ───────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that client_id and client_secret are present."""
        if not self._client_id or not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            connector_id=self.connector_id,
            message="Connector installed — complete OAuth2 authorization to connect",
        )

    def authorize(self) -> str:
        """Build and return the Xero OAuth2 PKCE authorization URL."""
        if not self._client_id:
            raise XeroAuthError("client_id is required to build authorization URL")

        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            hashlib.sha256(code_verifier.encode()).digest().hex()
        )
        # Store verifier in config for later token exchange
        self.config["_pkce_code_verifier"] = code_verifier

        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri or "",
            "scope": XERO_SCOPES,
            "state": self.connector_id or secrets.token_urlsafe(16),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{XERO_AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> Dict[str, Any]:
        """Exchange an OAuth2 authorization code for access + refresh tokens.

        Also fetches the Xero tenant ID from /connections and stores it in config.
        Returns the full token response dict.
        """
        code_verifier = self.config.get("_pkce_code_verifier", "")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                XERO_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._redirect_uri or "",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            body: dict[str, Any] = {}
            try:
                body = response.json()
            except Exception:
                pass
            err = body.get("error_description") or body.get("error") or response.text
            raise XeroAuthError(f"Token exchange failed: {err}", response.status_code)

        token_data: dict[str, Any] = response.json()
        access_token: str = token_data.get("access_token", "")

        # Fetch xero_tenant_id from /connections
        xero_tenant_id = await self._fetch_xero_tenant_id(access_token)

        self._access_token = access_token
        self._xero_tenant_id = xero_tenant_id
        self.config["access_token"] = access_token
        self.config["refresh_token"] = token_data.get("refresh_token", "")
        self.config["xero_tenant_id"] = xero_tenant_id
        self.http_client = self._make_client()

        return {**token_data, "xero_tenant_id": xero_tenant_id}

    async def _fetch_xero_tenant_id(self, access_token: str) -> str:
        """GET /connections and return the first tenant's tenantId."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                XERO_CONNECTIONS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )

        if response.status_code != 200:
            return ""

        connections: list[dict[str, Any]] = response.json()
        if connections:
            return connections[0].get("tenantId", "")
        return ""

    async def refresh_token(self) -> Dict[str, Any]:
        """Refresh the OAuth2 access token using the stored refresh token."""
        refresh_token = self.config.get("refresh_token", "")
        if not refresh_token:
            raise XeroAuthError("No refresh token available — re-authorize the connector")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                XERO_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            body: dict[str, Any] = {}
            try:
                body = response.json()
            except Exception:
                pass
            err = body.get("error_description") or body.get("error") or response.text
            raise XeroAuthError(f"Token refresh failed: {err}", response.status_code)

        token_data: dict[str, Any] = response.json()
        self._access_token = token_data.get("access_token", "")
        self.config["access_token"] = self._access_token
        if token_data.get("refresh_token"):
            self.config["refresh_token"] = token_data["refresh_token"]
        self.http_client = self._make_client()
        return token_data

    # ── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /Organisation and return current health."""
        if not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="No access token — complete OAuth2 authorization",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_organisation)
            await client.aclose()
            orgs: list[dict[str, Any]] = data.get("Organisations") or []
            org_name = orgs[0].get("Name", "") if orgs else ""
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Xero organisation: {org_name}",
                organisation_name=org_name,
            )
        except XeroAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.EXPIRED,
                message=str(exc),
            )
        except XeroNetworkError as exc:
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
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync Xero invoices, contacts, and accounts into the knowledge base.

        full=True → fetch all records.
        since=<datetime> → fetch records modified after that timestamp (RFC 7231 format).
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        modified_after: str | None = None
        if not full and since:
            modified_after = since.strftime("%a, %d %b %Y %H:%M:%S GMT")

        found = 0
        synced = 0
        failed = 0

        # Sync invoices
        try:
            inv_found, inv_synced, inv_failed = await self._sync_invoices(
                modified_after=modified_after, kb_id=kb_id
            )
            found += inv_found
            synced += inv_synced
            failed += inv_failed
        except XeroError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Invoice sync failed: {exc}",
            )

        # Sync contacts
        try:
            con_found, con_synced, con_failed = await self._sync_contacts(
                modified_after=modified_after, kb_id=kb_id
            )
            found += con_found
            synced += con_synced
            failed += con_failed
        except XeroError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Contact sync failed: {exc}",
            )

        # Sync accounts (no modified_after support)
        if full or since is None:
            try:
                acc_found, acc_synced, acc_failed = await self._sync_accounts(kb_id=kb_id)
                found += acc_found
                synced += acc_synced
                failed += acc_failed
            except XeroError as exc:
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

    async def _sync_invoices(
        self,
        modified_after: str | None,
        kb_id: str,
    ) -> tuple[int, int, int]:
        assert self.http_client is not None
        found = 0
        synced = 0
        failed = 0
        page = 1

        while True:
            data = await with_retry(
                self.http_client.list_invoices,
                modified_after=modified_after,
                page=page,
                page_size=SYNC_PAGE_SIZE,
            )
            invoices: list[dict[str, Any]] = data.get("Invoices") or []
            found += len(invoices)

            for inv in invoices:
                try:
                    doc = normalize_invoice(inv, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            if len(invoices) < SYNC_PAGE_SIZE:
                break
            page += 1

        return found, synced, failed

    async def _sync_contacts(
        self,
        modified_after: str | None,
        kb_id: str,
    ) -> tuple[int, int, int]:
        assert self.http_client is not None
        found = 0
        synced = 0
        failed = 0
        page = 1

        while True:
            data = await with_retry(
                self.http_client.list_contacts,
                modified_after=modified_after,
                page=page,
                page_size=SYNC_PAGE_SIZE,
            )
            contacts: list[dict[str, Any]] = data.get("Contacts") or []
            found += len(contacts)

            for con in contacts:
                try:
                    doc = normalize_contact(con, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            if len(contacts) < SYNC_PAGE_SIZE:
                break
            page += 1

        return found, synced, failed

    async def _sync_accounts(self, kb_id: str) -> tuple[int, int, int]:
        assert self.http_client is not None
        found = 0
        synced = 0
        failed = 0

        data = await with_retry(self.http_client.list_accounts)
        accounts: list[dict[str, Any]] = data.get("Accounts") or []
        found = len(accounts)

        for acc in accounts:
            try:
                doc = normalize_account(acc, self.connector_id, self._tenant_id)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        return found, synced, failed

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Invoices ─────────────────────────────────────────────────────────────

    async def list_invoices(self, modified_after: str | None = None) -> Dict[str, Any]:
        """GET /Invoices — paginated, optionally filtered by If-Modified-Since."""
        client = self._ensure_client()
        return await with_retry(client.list_invoices, modified_after=modified_after)

    async def get_invoice(self, invoice_id: str) -> Dict[str, Any]:
        """GET /Invoices/{invoice_id}."""
        client = self._ensure_client()
        return await with_retry(client.get_invoice, invoice_id)

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def list_contacts(self, modified_after: str | None = None) -> Dict[str, Any]:
        """GET /Contacts — paginated, optionally filtered by If-Modified-Since."""
        client = self._ensure_client()
        return await with_retry(client.list_contacts, modified_after=modified_after)

    async def get_contact(self, contact_id: str) -> Dict[str, Any]:
        """GET /Contacts/{contact_id}."""
        client = self._ensure_client()
        return await with_retry(client.get_contact, contact_id)

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def list_accounts(self) -> Dict[str, Any]:
        """GET /Accounts."""
        client = self._ensure_client()
        return await with_retry(client.list_accounts)

    async def get_connections(self) -> Dict[str, Any]:
        """GET https://api.xero.com/connections — returns list of tenants the user is connected to."""
        access_token = self._access_token
        if not access_token:
            raise XeroAuthError("No access token — complete OAuth2 authorization first")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    XERO_CONNECTIONS_URL,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise XeroNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise XeroNetworkError(f"Network error: {exc}") from exc

        if response.status_code == 200:
            connections = response.json()
            return {"connections": connections}

        body: Dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass
        err = body.get("Detail") or body.get("message") or response.text or "Unknown error"
        if response.status_code in (401, 403):
            raise XeroAuthError(f"Authentication failed: {err}", response.status_code)
        raise XeroNetworkError(f"Connections endpoint returned {response.status_code}: {err}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> XeroConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
