"""Constant Contact connector — orchestration only.

All HTTP calls  → client/http_client.py
All normalization + retry → helpers/utils.py
All models (standalone) → models.py

Imports BaseConnector via a try/except guard so this module loads cleanly
in the gateway's AST sandbox even when the Shielva SDK is absent.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(self, tenant_id: str = "", connector_id: str = "", config: Optional[Dict[str, Any]] = None) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

from client.http_client import ConstantContactHTTPClient
from exceptions import (
    ConstantContactAuthError,
    ConstantContactError,
    ConstantContactNetworkError,
)
from helpers.utils import normalize_campaign, normalize_contact, normalize_list, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

_AUTH_URL = "https://authz.constantcontact.com/oauth2/default/v1/authorize"
_TOKEN_URL = "https://authz.constantcontact.com/oauth2/default/v1/token"
_SCOPES = "contact_data campaign_data account_read"

SYNC_PAGE_LIMIT = 500


class ConstantContactConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Constant Contact.

    Provides OAuth2 authentication, health checks, full sync, and direct
    access to Constant Contact contacts, email campaigns, and contact lists
    via the v3 REST API.
    """

    CONNECTOR_TYPE: str = "constant_contact"
    AUTH_TYPE: str = "oauth2"
    CONNECTOR_NAME: str = "Constant Contact"
    AUTH_URL: str = _AUTH_URL
    TOKEN_URL: str = _TOKEN_URL
    SCOPES: str = _SCOPES

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=cfg)
        self._http_client: Optional[ConstantContactHTTPClient] = None

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _has_credentials(self) -> bool:
        """Return True when both client_id and client_secret are present."""
        return bool(
            self.config.get("client_id") and self.config.get("client_secret")
        )

    def _has_token(self) -> bool:
        """Return True when an access token is stored in config."""
        return bool(self.config.get("access_token"))

    def _make_client(self) -> ConstantContactHTTPClient:
        """Construct a fresh HTTP client from stored config."""
        return ConstantContactHTTPClient(
            access_token=self.config.get("access_token", ""),
            refresh_token=self.config.get("refresh_token", ""),
            client_id=self.config.get("client_id", ""),
            client_secret=self.config.get("client_secret", ""),
        )

    def _ensure_client(self) -> ConstantContactHTTPClient:
        """Return (and lazily create) the HTTP client."""
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install-time config: client_id and client_secret are required.

        Returns InstallResult. Does not make any API calls — the OAuth2 flow
        is initiated separately via authorize().
        """
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        if not client_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="client_id is required",
            )
        if not client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="client_secret is required",
            )

        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            connector_id=self.connector_id,
            message="Connector installed — complete OAuth to connect",
        )

    # ── authorize ─────────────────────────────────────────────────────────────

    async def authorize(
        self,
        redirect_uri: Optional[str] = None,
        state: Optional[str] = None,
    ) -> str:
        """Return the Constant Contact OAuth2 authorization URL.

        The caller must redirect the user to this URL to complete the OAuth2
        Authorization Code flow. The returned URL includes the client_id,
        response_type=code, and the requested scopes.
        """
        client_id = self.config.get("client_id", "")
        ru = redirect_uri or self.config.get("redirect_uri", "")

        params: Dict[str, str] = {
            "response_type": "code",
            "client_id": client_id,
            "scope": _SCOPES,
        }
        if ru:
            params["redirect_uri"] = ru
        if state:
            params["state"] = state

        return f"{_AUTH_URL}?{urlencode(params)}"

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Check Constant Contact API connectivity by fetching account summary.

        Returns HealthCheckResult with organization name when healthy.
        """
        if not self._has_token():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="No access token — complete OAuth to connect",
            )

        try:
            client = self._ensure_client()
            account = await with_retry(client.get_account_info)
            org_name: str = (
                account.get("organization_name", "")
                or account.get("physical_address", {}).get("organization", "")
                or ""
            )
            first: str = account.get("first_name", "") or ""
            last: str = account.get("last_name", "") or ""
            user_name: str = f"{first} {last}".strip()
            msg = f"Connected to {org_name}" if org_name else "Constant Contact API reachable"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
                organization_name=org_name,
                user_name=user_name,
            )
        except ConstantContactAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Token expired or invalid: {exc.message}",
            )
        except ConstantContactNetworkError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=f"Network error: {exc.message}",
            )
        except ConstantContactError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── sync ─────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Full sync of Constant Contact contacts and email campaigns.

        Fetches all contacts and campaigns using cursor-based pagination.
        Normalizes each resource into a ConnectorDocument.
        Returns SyncResult with counts.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            client = self._ensure_client()

            # ── Sync contacts ───────────────────────────────────────────────
            cursor: Optional[str] = None
            while True:
                resp = await with_retry(client.get_contacts, cursor=cursor, limit=SYNC_PAGE_LIMIT)
                contacts: List[Dict[str, Any]] = resp.get("contacts", [])
                for contact in contacts:
                    documents_found += 1
                    try:
                        normalize_contact(contact, self.connector_id, self.tenant_id)
                        documents_synced += 1
                    except Exception:
                        documents_failed += 1

                links = resp.get("_links")
                cursor = ConstantContactHTTPClient._extract_cursor(links)
                if not cursor:
                    break

            # ── Sync email campaigns ─────────────────────────────────────────
            cursor = None
            while True:
                resp = await with_retry(client.get_email_campaigns, cursor=cursor)
                campaigns: List[Dict[str, Any]] = resp.get("campaigns", [])
                for campaign in campaigns:
                    documents_found += 1
                    try:
                        normalize_campaign(campaign, self.connector_id, self.tenant_id)
                        documents_synced += 1
                    except Exception:
                        documents_failed += 1

                links = resp.get("_links")
                cursor = ConstantContactHTTPClient._extract_cursor(links)
                if not cursor:
                    break

            status = (
                SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} records",
            )

        except Exception as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Convenience methods ───────────────────────────────────────────────────

    async def list_contacts(
        self,
        cursor: Optional[str] = None,
        limit: int = SYNC_PAGE_LIMIT,
    ) -> List[ConnectorDocument]:
        """Return a page of contacts as normalized ConnectorDocuments."""
        client = self._ensure_client()
        resp = await with_retry(client.get_contacts, cursor=cursor, limit=limit)
        contacts: List[Dict[str, Any]] = resp.get("contacts", [])
        return [
            normalize_contact(c, self.connector_id, self.tenant_id)
            for c in contacts
        ]

    async def list_campaigns(
        self, cursor: Optional[str] = None
    ) -> List[ConnectorDocument]:
        """Return a page of email campaigns as normalized ConnectorDocuments."""
        client = self._ensure_client()
        resp = await with_retry(client.get_email_campaigns, cursor=cursor)
        campaigns: List[Dict[str, Any]] = resp.get("campaigns", [])
        return [
            normalize_campaign(c, self.connector_id, self.tenant_id)
            for c in campaigns
        ]

    async def list_contact_lists(
        self, cursor: Optional[str] = None
    ) -> List[ConnectorDocument]:
        """Return a page of contact lists as normalized ConnectorDocuments."""
        client = self._ensure_client()
        resp = await with_retry(client.get_contact_lists, cursor=cursor)
        lists: List[Dict[str, Any]] = resp.get("lists", [])
        return [
            normalize_list(lst, self.connector_id, self.tenant_id)
            for lst in lists
        ]

    async def get_contact(self, contact_id: str) -> ConnectorDocument:
        """Fetch and normalize a single contact by ID."""
        client = self._ensure_client()
        raw = await with_retry(client.get_contact, contact_id)
        return normalize_contact(raw, self.connector_id, self.tenant_id)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Release any held async resources."""
        self._http_client = None

    async def __aenter__(self) -> "ConstantContactConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
