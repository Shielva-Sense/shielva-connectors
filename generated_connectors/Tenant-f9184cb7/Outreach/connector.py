from __future__ import annotations

import urllib.parse
from datetime import datetime
from typing import Any

from client import OutreachHTTPClient
from exceptions import OutreachAuthError, OutreachError, OutreachNetworkError
from helpers import (
    normalize_account,
    normalize_prospect,
    normalize_sequence,
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

CONNECTOR_TYPE: str = "outreach"
AUTH_TYPE: str = "oauth2"

OUTREACH_AUTH_URL: str = "https://api.outreach.io/oauth/authorize"
OUTREACH_TOKEN_URL: str = "https://api.outreach.io/oauth/token"
OUTREACH_SCOPES: str = (
    "prospects.all sequences.read accounts.read mailings.read calls.read"
)
SYNC_PAGE_SIZE: int = 100


class OutreachConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Outreach sales engagement platform.

    Provides OAuth2 Authorization Code flow authentication, health checks,
    full sync of prospects, sequences, and accounts, and direct access to
    calls, mailings, and individual prospects via the Outreach JSON:API.
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
            super().__init__(  # type: ignore[misc]
                tenant_id=tenant_id,
                connector_id=connector_id,
                config=_config,
            )
        except TypeError:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = _config

        self._access_token: str = _config.get("access_token", "")
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._http_client: OutreachHTTPClient | None = None

    def _make_client(self) -> OutreachHTTPClient:
        return OutreachHTTPClient(config=self.config)

    def _ensure_client(self) -> OutreachHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _has_token(self) -> bool:
        return bool(self._access_token)

    def _has_oauth_credentials(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # ── OAuth2 ────────────────────────────────────────────────────────────────

    async def authorize(self) -> str:
        """Build and return the OAuth2 authorization URL for Outreach."""
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
            "scope": OUTREACH_SCOPES,
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        query_string = urllib.parse.urlencode(params)
        return f"{OUTREACH_AUTH_URL}?{query_string}"

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that client_id and client_secret are present."""
        if not self._client_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id is required — copy it from your Outreach OAuth app settings.",
            )
        if not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_secret is required — copy it from your Outreach OAuth app settings.",
            )
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Outreach OAuth2 credentials accepted. Redirect user to the authorize URL to complete OAuth flow.",
        )

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /api/v2/users/current and return current health status."""
        if not self._has_token():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required — complete the OAuth2 flow first.",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_current_user)
            user_data: dict[str, Any] = data.get("data", {}) or {}
            attrs: dict[str, Any] = user_data.get("attributes", {}) or {}
            display: str = (
                attrs.get("email", "")
                or attrs.get("name", "")
                or f"User {user_data.get('id', '')}"
            )
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Outreach API reachable. User: {display}",
            )
        except OutreachAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except OutreachNetworkError as exc:
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
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync Outreach prospects, sequences, and accounts into the knowledge base.

        Paginates through all resources using JSON:API ``links.next`` cursor
        pagination and normalizes each record into a ConnectorDocument.
        """
        _ = full, since
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        for fetch_fn, normalize_fn in (
            (self._fetch_all_prospects, normalize_prospect),
            (self._fetch_all_sequences, normalize_sequence),
            (self._fetch_all_accounts, normalize_account),
        ):
            try:
                records = await fetch_fn(client)
            except OutreachError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            found += len(records)
            for record in records:
                try:
                    doc = normalize_fn(record, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_prospects(
        self, client: OutreachHTTPClient
    ) -> list[dict[str, Any]]:
        return await self._paginate(client.get_prospects)

    async def _fetch_all_sequences(
        self, client: OutreachHTTPClient
    ) -> list[dict[str, Any]]:
        return await self._paginate(client.get_sequences)

    async def _fetch_all_accounts(
        self, client: OutreachHTTPClient
    ) -> list[dict[str, Any]]:
        return await self._paginate(client.get_accounts)

    async def _paginate(
        self,
        fetch_fn: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Paginate a JSON:API endpoint using links.next cursor pagination."""
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(fetch_fn, cursor=cursor, **kwargs)
            data = page.get("data") or []
            if not isinstance(data, list):
                data = [data]
            if not data:
                break
            records.extend(data)
            links: dict[str, Any] = page.get("links") or {}
            next_url: str | None = links.get("next")
            if not next_url:
                break
            cursor = next_url
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Prospects ─────────────────────────────────────────────────────────────

    async def list_prospects(
        self,
        cursor: str | None = None,
        count: int = SYNC_PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        """Return one page of Outreach prospects."""
        client = self._ensure_client()
        data = await with_retry(client.get_prospects, cursor=cursor, count=count)
        result = data.get("data") or []
        return result if isinstance(result, list) else [result]

    async def get_prospect(self, prospect_id: int | str) -> dict[str, Any]:
        """Return a single Outreach prospect by ID."""
        client = self._ensure_client()
        data = await with_retry(client.get_prospect, prospect_id)
        return data.get("data") or data

    # ── Sequences ─────────────────────────────────────────────────────────────

    async def list_sequences(
        self,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one page of Outreach sequences."""
        client = self._ensure_client()
        data = await with_retry(client.get_sequences, cursor=cursor)
        result = data.get("data") or []
        return result if isinstance(result, list) else [result]

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def list_accounts(
        self,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one page of Outreach accounts."""
        client = self._ensure_client()
        data = await with_retry(client.get_accounts, cursor=cursor)
        result = data.get("data") or []
        return result if isinstance(result, list) else [result]

    # ── Calls ─────────────────────────────────────────────────────────────────

    async def list_calls(
        self,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one page of Outreach calls."""
        client = self._ensure_client()
        data = await with_retry(client.get_calls, cursor=cursor)
        result = data.get("data") or []
        return result if isinstance(result, list) else [result]

    # ── Mailings ──────────────────────────────────────────────────────────────

    async def list_mailings(
        self,
        limit: int = SYNC_PAGE_SIZE,
        offset: int = 0,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one page of Outreach mailings."""
        client = self._ensure_client()
        data = await with_retry(client.get_mailings, cursor=cursor)
        result = data.get("data") or []
        return result if isinstance(result, list) else [result]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> OutreachConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
