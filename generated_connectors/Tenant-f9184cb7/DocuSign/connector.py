from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from client.http_client import (
    DocuSignHTTPClient,
    build_oauth_url,
    exchange_code_for_token,
    fetch_user_info,
    refresh_access_token,
)
from exceptions import DocuSignAuthError, DocuSignError, DocuSignNetworkError
from helpers.utils import normalize_envelope, with_retry
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

CONNECTOR_TYPE: str = "docusign"
SYNC_PAGE_SIZE = 100
DEFAULT_SYNC_DAYS = 30
DEFAULT_REDIRECT_URI = "https://app.shielva.ai/oauth/callback/docusign"


class DocuSignConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for DocuSign eSignature REST API v2.1.

    Provides OAuth2 Authorization Code Grant authentication, health checks,
    envelope sync, and direct access to DocuSign envelope resources.
    """

    CONNECTOR_TYPE: str = "docusign"
    AUTH_TYPE: str = "oauth2"

    def _is_sandbox(self) -> bool:
        """Return True if using the DocuSign sandbox (demo) environment."""
        return bool(self.config.get("is_sandbox", True))

    def _base_oauth_url(self) -> str:
        """Return the DocuSign OAuth base URL for sandbox or production."""
        return (
            "https://account-d.docusign.com"
            if self._is_sandbox()
            else "https://account.docusign.com"
        )

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(
                tenant_id=tenant_id,
                connector_id=connector_id,
                config=_config,
            )
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        # DocuSign OAuth2 config fields
        self._integration_key: str = _config.get("integration_key", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = (
            _config.get("redirect_uri", "") or DEFAULT_REDIRECT_URI
        )

        # Post-OAuth fields (populated after authorize + callback)
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")
        self._account_id: str = _config.get("account_id", "")
        self._base_uri: str = _config.get("base_uri", "")

        self._http_client: Optional[DocuSignHTTPClient] = None

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate integration_key and client_secret are present."""
        if not self._integration_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="integration_key is required",
            )
        if not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_secret is required",
            )

        # If we already have a valid access token, report connected
        if self._access_token and self._account_id and self._base_uri:
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or self._account_id,
                message=f"DocuSign credentials validated. Account: {self._account_id}",
            )

        # Credentials present but OAuth not yet completed
        return InstallResult(
            health=ConnectorHealth.DEGRADED,
            auth_status=AuthStatus.PENDING_OAUTH,
            connector_id=self.connector_id,
            message=(
                "Credentials accepted. Complete OAuth2 authorization "
                "by calling authorize() and completing the browser flow."
            ),
        )

    # ── OAuth2 ────────────────────────────────────────────────────────────────

    def authorize(self, state: str = "") -> str:
        """
        Return the DocuSign OAuth2 Authorization Code Grant URL.

        The user must open this URL in a browser, log in, and grant consent.
        DocuSign will redirect to redirect_uri with ?code=<auth_code>.
        Uses sandbox (account-d.docusign.com) when is_sandbox=True (default).
        """
        if not self._integration_key:
            raise DocuSignAuthError(
                "integration_key is required to build the OAuth URL"
            )
        return build_oauth_url(
            integration_key=self._integration_key,
            redirect_uri=self._redirect_uri,
            state=state,
            base_oauth_url=self._base_oauth_url(),
        )

    async def handle_oauth_callback(self, code: str) -> dict[str, Any]:
        """
        Exchange an authorization code for tokens and fetch account info.

        After calling this method, account_id and base_uri are stored in
        config and the connector is ready to make API calls.

        Returns the token response dict for persistence by the caller.
        """
        tokens = await exchange_code_for_token(
            integration_key=self._integration_key,
            client_secret=self._client_secret,
            code=code,
            redirect_uri=self._redirect_uri,
        )
        access_token: str = tokens["access_token"]
        refresh_token: str = tokens.get("refresh_token", "")

        # Fetch account info
        user_info = await fetch_user_info(access_token)
        accounts: list[dict[str, Any]] = user_info.get("accounts", [])
        if not accounts:
            raise DocuSignAuthError("No DocuSign accounts found for this user")

        # Use first (default) account
        account = accounts[0]
        account_id: str = account.get("account_id", "")
        base_uri: str = account.get("base_uri", "")

        # Persist into config
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._account_id = account_id
        self._base_uri = base_uri
        self.config.update(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "account_id": account_id,
                "base_uri": base_uri,
            }
        )

        # Reset client so next call picks up new token
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
            "base_uri": base_uri,
        }

    async def _maybe_refresh_token(self) -> None:
        """Attempt a token refresh if refresh_token is present."""
        if not self._refresh_token:
            return
        tokens = await refresh_access_token(
            integration_key=self._integration_key,
            client_secret=self._client_secret,
            refresh_token=self._refresh_token,
        )
        self._access_token = tokens["access_token"]
        if "refresh_token" in tokens:
            self._refresh_token = tokens["refresh_token"]
        self.config.update(
            {
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
            }
        )
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """GET /accounts/{account_id} and return health with account name."""
        if not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="No access token — complete OAuth2 flow first",
            )
        client = self._ensure_client()
        try:
            data = await with_retry(client.get_account)
            account_name: str = data.get("accountName", "") or data.get(
                "account_name", ""
            )
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="DocuSign API is reachable",
                account_name=account_name,
            )
        except DocuSignAuthError as exc:
            # Try token refresh on 401
            if exc.status_code == 401 and self._refresh_token:
                try:
                    await self._maybe_refresh_token()
                    client = self._ensure_client()
                    data = await with_retry(client.get_account)
                    account_name = data.get("accountName", "")
                    return HealthCheckResult(
                        health=ConnectorHealth.HEALTHY,
                        auth_status=AuthStatus.CONNECTED,
                        message="DocuSign API is reachable (token refreshed)",
                        account_name=account_name,
                    )
                except Exception:
                    pass
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except DocuSignNetworkError as exc:
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
        since: Optional[datetime] = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync DocuSign envelopes into the knowledge base.

        Fetches envelopes from the past 30 days by default.
        full=True → fetch from the very beginning (no date filter).
        since=<datetime> → fetch envelopes created after that timestamp.
        """
        if not self._access_token:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="No access token — complete OAuth2 flow first",
            )

        client = self._ensure_client()

        # Build from_date
        from_date: Optional[str] = None
        if not full:
            if since:
                from_date = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                cutoff = datetime.now(tz=timezone.utc) - timedelta(
                    days=DEFAULT_SYNC_DAYS
                )
                from_date = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        found = 0
        synced = 0
        failed = 0
        start_position = 0

        while True:
            try:
                page = await with_retry(
                    client.list_envelopes,
                    from_date=from_date,
                    status="completed",
                    count=SYNC_PAGE_SIZE,
                    start_position=start_position,
                )
            except DocuSignError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            envelopes: list[dict[str, Any]] = page.get("envelopes", [])
            total_set_size = int(page.get("totalSetSize", len(envelopes)))
            result_set_size = int(page.get("resultSetSize", len(envelopes)))
            found += len(envelopes)

            for envelope in envelopes:
                try:
                    doc = normalize_envelope(
                        envelope, self.connector_id, self._tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # DocuSign pagination: stop when we've read all results
            if result_set_size < SYNC_PAGE_SIZE or found >= total_set_size:
                break
            start_position += result_set_size

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(
        self, doc: ConnectorDocument, kb_id: str
    ) -> None:
        """Push a normalized document into the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Envelope operations ───────────────────────────────────────────────────

    async def list_envelopes(
        self,
        from_date: Optional[str] = None,
        status: str = "completed",
        count: int = 100,
        start_position: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        List envelopes for the account.

        Args:
            from_date: ISO 8601 date string (e.g. "2026-01-01T00:00:00Z")
            status: Envelope status filter (completed, sent, delivered, etc.)
            count: Number of envelopes per page (max 100)
            start_position: Pagination offset
        """
        client = self._ensure_client()
        return await with_retry(
            client.list_envelopes,
            from_date=from_date,
            status=status,
            count=count,
            start_position=start_position,
            **kwargs,
        )

    async def get_envelope(self, envelope_id: str) -> dict[str, Any]:
        """Get a single envelope by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_envelope, envelope_id)

    async def list_envelope_documents(
        self, envelope_id: str
    ) -> dict[str, Any]:
        """List documents attached to an envelope."""
        client = self._ensure_client()
        return await with_retry(client.list_envelope_documents, envelope_id)

    async def list_envelope_recipients(
        self, envelope_id: str
    ) -> dict[str, Any]:
        """List recipients (signers, carbon copies, etc.) for an envelope."""
        client = self._ensure_client()
        return await with_retry(
            client.list_envelope_recipients, envelope_id
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_client(self) -> DocuSignHTTPClient:
        if self._http_client is None:
            if not self._access_token:
                raise DocuSignAuthError(
                    "No access token available — complete OAuth2 flow first"
                )
            if not self._account_id or not self._base_uri:
                raise DocuSignAuthError(
                    "account_id and base_uri are required — complete OAuth2 flow first"
                )
            self._http_client = DocuSignHTTPClient(
                access_token=self._access_token,
                base_uri=self._base_uri,
                account_id=self._account_id,
            )
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> DocuSignConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
