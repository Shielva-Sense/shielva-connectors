"""Nutshell connector — orchestration only.

All HTTP calls → client/http_client.py (JSON-RPC 2.0 over HTTPS, HTTP Basic auth).
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Nutshell is a JSON-RPC 2.0 API at ``https://app.nutshell.com/api/v1/json``. The
wire envelope is:

    {"jsonrpc": "2.0", "id": <int>, "method": "<rpcName>", "params": {...}}

Authentication is HTTP Basic — ``username`` (login email) + ``api_key`` (issued
from Nutshell → Setup → API Keys). Errors arrive in *two* shapes:

  * HTTP-level (401, 404, 429, 5xx).
  * HTTP 200 with ``{"jsonrpc": "2.0", "id": N, "error": {...}}``.

The client parses both — this orchestrator only catches typed exceptions.
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
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import NutshellHTTPClient
from exceptions import (
    NutshellAuthError,
    NutshellError,
    NutshellNetworkError,
    NutshellNotFound,
)
from helpers.normalizer import normalize_account, normalize_contact, normalize_lead
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_NUTSHELL_BASE = "https://app.nutshell.com/api/v1/json"


class NutshellConnector(BaseConnector):
    """Shielva connector for the Nutshell SMB sales CRM (JSON-RPC 2.0)."""

    # Class attributes — these MUST live on the subclass (not just module-level
    # constants) because the gateway loader inspects ``cls.CONNECTOR_TYPE`` and
    # ``cls.AUTH_TYPE`` for connector registration.
    CONNECTOR_TYPE = "nutshell"
    CONNECTOR_NAME = "Nutshell"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "email",
        "api_key",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=config)
        # ``email`` is the canonical install_field key; ``username`` is honoured
        # as a backwards-compat alias for already-stored sessions.
        self.username: str = (
            self.config.get("email")
            or self.config.get("username")
            or ""
        )
        self.api_key: str = self.config.get("api_key", "")
        self.base_url: str = self.config.get("base_url") or _NUTSHELL_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = NutshellHTTPClient(
            base_url=self.base_url,
            username=self.username,
            api_key=self.api_key,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and confirm credentials by calling ``getUser``.

        The Nutshell ``getUser`` RPC with empty params returns the authenticated
        user; it's the cheapest credential probe available and acts as the
        install-time canary.
        """
        username = (
            self.config.get("email")
            or self.config.get("username")
            or ""
        )
        api_key = self.config.get("api_key", "")

        if not username or not api_key:
            logger.warning(
                "nutshell.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required config: email and api_key are both required",
            )

        try:
            await self.http_client.get_current_user()
        except NutshellAuthError as exc:
            logger.warning(
                "nutshell.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Authentication failed: {exc.message}",
            )
        except NutshellError as exc:
            logger.warning(
                "nutshell.install.failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.PENDING,
                message=f"Install failed: {exc.message}",
            )

        await self.save_config({"email": username, "api_key": api_key})
        logger.info("nutshell.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Nutshell connector installed and credentials verified",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose ``access_token`` carries the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Confirm Nutshell API connectivity by fetching the current user."""
        try:
            await with_retry(
                lambda: self.http_client.get_current_user(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Nutshell API reachable",
            )
        except NutshellAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Authentication failed: {exc.message}",
            )
        except NutshellNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.AUTHENTICATED,
                message=f"Network error: {exc.message}",
            )
        except NutshellError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.AUTHENTICATED,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
        **kwargs: Any,
    ) -> SyncResult:
        """Page through Nutshell contacts + leads + accounts and report counts.

        Nutshell does not expose a delta/changes feed via JSON-RPC, so this is
        a windowed sync: page through contacts, leads, and accounts at the
        configured page size and report the totals. A failure on any single
        resource type is recorded as PARTIAL.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            contacts = await self.list_contacts(page=1, limit=50)
            documents_found += len(contacts)
            documents_synced += len(contacts)
        except NutshellError as exc:
            logger.warning("nutshell.sync.contacts_failed", error=str(exc))
            documents_failed += 1

        try:
            leads = await self.list_leads(page=1, limit=50)
            documents_found += len(leads)
            documents_synced += len(leads)
        except NutshellError as exc:
            logger.warning("nutshell.sync.leads_failed", error=str(exc))
            documents_failed += 1

        try:
            accounts = await self.list_accounts(page=1, limit=50)
            documents_found += len(accounts)
            documents_synced += len(accounts)
        except NutshellError as exc:
            logger.warning("nutshell.sync.accounts_failed", error=str(exc))
            documents_failed += 1

        status = SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=documents_found,
            documents_synced=documents_synced,
            documents_failed=documents_failed,
            message=f"Synced {documents_synced} records ({documents_failed} resource failures)",
        )

    # ── Contacts ───────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        page: int = 1,
        limit: int = 50,
        query: Optional[Dict[str, Any]] = None,
        order_by: str = "lastName",
    ) -> List[Dict[str, Any]]:
        """Return a page of contacts as normalized dicts.

        ``query`` is a Nutshell-style filter object (see API docs); passed
        through unchanged when supplied. ``order_by`` defaults to ``lastName``.
        """
        raw = await with_retry(
            lambda: self.http_client.find_contacts(
                page=page, limit=limit, query=query, order_by=order_by
            ),
            max_retries=3,
        )
        items = raw if isinstance(raw, list) else (raw or {}).get("results") or []
        return [normalize_contact(item) for item in items]

    async def get_contact(
        self,
        contact_id: int,
        contact_rev: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch and normalize a single contact by Nutshell ID."""
        raw = await with_retry(
            lambda: self.http_client.get_contact(contact_id, contact_rev),
            max_retries=3,
        )
        return normalize_contact(raw or {})

    async def create_contact(self, contact: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new contact. Returns the normalized created record."""
        raw = await self.http_client.new_contact(contact)
        return normalize_contact(raw or {})

    async def update_contact(
        self,
        contact_id: int,
        rev: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Patch a contact. ``rev`` is required for Nutshell's optimistic locking."""
        raw = await self.http_client.edit_contact(contact_id, rev, fields)
        return normalize_contact(raw or {})

    async def delete_contact(self, contact_id: int, rev: str) -> Dict[str, Any]:
        """Delete a contact by ID + rev. Returns a delete envelope."""
        result = await self.http_client.delete_contact(contact_id, rev)
        return {"deleted": True, "contact_id": contact_id, "result": result}

    # ── Leads ──────────────────────────────────────────────────────────────

    async def list_leads(
        self,
        page: int = 1,
        limit: int = 50,
        query: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Page through Nutshell leads with an optional filter."""
        raw = await with_retry(
            lambda: self.http_client.find_leads(page=page, limit=limit, query=query),
            max_retries=3,
        )
        items = raw if isinstance(raw, list) else (raw or {}).get("results") or []
        return [normalize_lead(item) for item in items]

    async def create_lead(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new Nutshell lead. Returns the normalized created lead."""
        raw = await self.http_client.new_lead(lead)
        return normalize_lead(raw or {})

    # ── Accounts ───────────────────────────────────────────────────────────

    async def list_accounts(
        self,
        page: int = 1,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Page through Nutshell accounts (companies)."""
        raw = await with_retry(
            lambda: self.http_client.find_accounts(page=page, limit=limit),
            max_retries=3,
        )
        items = raw if isinstance(raw, list) else (raw or {}).get("results") or []
        return [normalize_account(item) for item in items]

    # ── Activities + Users ─────────────────────────────────────────────────

    async def list_activities(
        self,
        page: int = 1,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Page through Nutshell activities (calls / meetings / notes)."""
        raw = await with_retry(
            lambda: self.http_client.find_activities(page=page, limit=limit),
            max_retries=3,
        )
        if isinstance(raw, list):
            return raw
        return (raw or {}).get("results") or []

    async def log_activity(self, activity: Dict[str, Any]) -> Dict[str, Any]:
        """Log a Nutshell activity (call / meeting / note)."""
        raw = await self.http_client.new_activity(activity)
        return raw or {}

    async def list_users(self) -> List[Dict[str, Any]]:
        """Return the list of Nutshell seats — useful for assignment."""
        raw = await self.http_client.find_users()
        if isinstance(raw, list):
            return raw
        return (raw or {}).get("results") or []
