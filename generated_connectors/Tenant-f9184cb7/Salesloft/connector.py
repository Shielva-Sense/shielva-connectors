from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from urllib.parse import urlencode

from client.http_client import SalesloftHTTPClient
from exceptions import SalesloftAuthError, SalesloftError, SalesloftNetworkError
from helpers.utils import (
    CircuitBreaker,
    normalize_cadence,
    normalize_call,
    normalize_person,
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
            config: Dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

SALESLOFT_AUTH_URL = "https://accounts.salesloft.com/oauth/authorize"
SALESLOFT_TOKEN_URL = "https://accounts.salesloft.com/oauth/token"
SALESLOFT_SCOPES = "read"
SYNC_PAGE_SIZE = 50
CIRCUIT_BREAKER_THRESHOLD = 5


class SalesloftConnector(BaseConnector):
    """
    Shielva connector for Salesloft sales engagement platform.

    Provides OAuth2 authorization, health checks, full sync, and direct access
    to Salesloft people, cadences, calls, emails, and accounts via the v2 REST API.
    Authentication uses OAuth2 Authorization Code flow; requests are authorized
    with ``Authorization: Bearer {access_token}``.
    """

    CONNECTOR_TYPE: str = "salesloft"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        # Salesloft-specific attrs
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")
        self._token_expires_at: str = _config.get("token_expires_at", "")
        self.http_client: SalesloftHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> SalesloftHTTPClient:
        return SalesloftHTTPClient(
            access_token=self._access_token,
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=self._redirect_uri,
        )

    def _has_token(self) -> bool:
        return bool(self._access_token)

    def _has_credentials(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # ── Auth ────────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that client_id and client_secret are present."""
        if not self._client_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id is required — register your app at https://developers.salesloft.com",
            )
        if not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_secret is required — register your app at https://developers.salesloft.com",
            )
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            connector_id=self.connector_id,
            message="Connector installed — complete OAuth2 authorization to connect",
        )

    def authorize(self) -> str:
        """Build and return the Salesloft OAuth2 authorization URL."""
        if not self._client_id:
            raise SalesloftAuthError("client_id is required to build authorization URL")

        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
            "scope": SALESLOFT_SCOPES,
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        if self.connector_id:
            params["state"] = self.connector_id

        return f"{SALESLOFT_AUTH_URL}?{urlencode(params)}"

    # ── Health ──────────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /v2/me.json and return current health."""
        if not self._has_token():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required — complete OAuth2 flow first",
            )
        client = self._make_client()
        try:
            me = await with_retry(client.get_me)
            await client.aclose()
            self._circuit_breaker.on_success()
            me_data: dict[str, Any] = me.get("data", me) if isinstance(me, dict) else {}
            name = str(me_data.get("name", "") or "")
            email = str(me_data.get("email", "") or "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Salesloft API is reachable",
                name=name,
                email=email,
            )
        except SalesloftAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except SalesloftNetworkError as exc:
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

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """
        Sync Salesloft people, cadences, and calls into the knowledge base.

        Salesloft uses page-based pagination (page + per_page).
        ``since`` is accepted for API compatibility — the connector fetches all
        pages regardless (Salesloft list APIs do not support server-side
        timestamp filtering on the base list endpoints).
        """
        _ = since
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        for fetch_fn, normalize_fn in (
            (self._fetch_all_people, normalize_person),
            (self._fetch_all_cadences, normalize_cadence),
            (self._fetch_all_calls, normalize_call),
        ):
            try:
                records = await fetch_fn()
            except SalesloftError as exc:
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

    async def _fetch_all_people(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        return await self._fetch_all_pages(self.http_client.get_people)

    async def _fetch_all_cadences(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        return await self._fetch_all_pages(self.http_client.get_cadences)

    async def _fetch_all_calls(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        return await self._fetch_all_pages(self.http_client.get_activities_calls)

    async def _fetch_all_pages(
        self,
        list_fn: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Paginate through Salesloft page-based pages until exhausted."""
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await with_retry(list_fn, page=page, per_page=SYNC_PAGE_SIZE, **kwargs)
            data: list[dict[str, Any]] = response.get("data") or []
            if not data:
                break
            records.extend(data)
            paging: dict[str, Any] = (response.get("metadata") or {}).get("paging") or {}
            next_page = paging.get("next_page")
            if not next_page:
                break
            page = int(next_page)
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── People ───────────────────────────────────────────────────────────────

    async def list_people(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_people, page=page, per_page=per_page)

    # ── Cadences ─────────────────────────────────────────────────────────────

    async def list_cadences(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_cadences, page=page, per_page=per_page)

    # ── Calls ────────────────────────────────────────────────────────────────

    async def list_calls(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_activities_calls, page=page, per_page=per_page)

    # ── Emails ───────────────────────────────────────────────────────────────

    async def list_emails(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_emails, page=page, per_page=per_page)

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def list_accounts(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_accounts, page=page, per_page=per_page)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> SalesloftHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> SalesloftConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
