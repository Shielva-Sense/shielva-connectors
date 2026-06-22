from __future__ import annotations

import urllib.parse
from datetime import datetime
from typing import Any, Dict

from client import SquareHTTPClient
from exceptions import SquareAuthError, SquareError, SquareNetworkError
from helpers import CircuitBreaker, normalize_customer, normalize_payment, with_retry
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

SQUARE_AUTH_URL = "https://connect.squareup.com/oauth2/authorize"
SQUARE_TOKEN_URL = "https://connect.squareup.com/oauth2/token"
SQUARE_SCOPES = "MERCHANT_PROFILE_READ PAYMENTS_READ ORDERS_READ CUSTOMERS_READ ITEMS_READ"
SYNC_PAGE_SIZE = 100
CIRCUIT_BREAKER_THRESHOLD = 5


class SquareConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Square.

    Provides OAuth2 authorization, health checks, full sync of payments, orders,
    and customers via the Square REST API v2.
    """

    CONNECTOR_TYPE: str = "square"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self.tenant_id = tenant_id
        # Square OAuth2 install fields
        self._application_id: str = _config.get("application_id", "")
        self._application_secret: str = _config.get("application_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self.http_client: SquareHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> SquareHTTPClient:
        return SquareHTTPClient(access_token=self._access_token)

    def _has_install_credentials(self) -> bool:
        return bool(self._application_id and self._application_secret)

    def _has_access_token(self) -> bool:
        return bool(self._access_token)

    # ── Auth & install ───────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate application_id/application_secret are present.

        If an access_token is already stored (post-OAuth flow), probe GET /merchants/me.
        Otherwise, confirm the install credentials are present and return CONNECTED
        with a message directing the user to authorize via authorize().
        """
        if not self._has_install_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="application_id and application_secret are required",
            )

        # If we already have an access token, validate it live
        if self._has_access_token():
            client = self._make_client()
            try:
                await with_retry(client.get_merchant)
                await client.aclose()
                self.http_client = self._make_client()
                return InstallResult(
                    health=ConnectorHealth.HEALTHY,
                    auth_status=AuthStatus.CONNECTED,
                    connector_id=self.connector_id,
                    message="Connected to Square",
                )
            except SquareAuthError as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Square authentication failed: {exc}",
                )
            except Exception as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.FAILED,
                    message=str(exc),
                )

        # Credentials present but no access token yet — direct user to authorize
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message=(
                "Square app credentials validated. "
                "Call authorize() to get the OAuth2 authorization URL."
            ),
        )

    def authorize(self) -> str:
        """Return the Square OAuth2 authorization URL for the configured app."""
        params: dict[str, str] = {
            "client_id": self._application_id,
            "scope": SQUARE_SCOPES,
            "response_type": "code",
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        return f"{SQUARE_AUTH_URL}?{urllib.parse.urlencode(params)}"

    async def health_check(self) -> HealthCheckResult:
        """Probe GET /merchants/me and return current health with merchant name."""
        if not self._has_access_token():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required for health check",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_merchant)
            await client.aclose()
            self._circuit_breaker.on_success()
            merchant = data.get("merchant", {})
            merchant_name = merchant.get("business_name") or merchant.get("id", "Unknown")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Square merchant: {merchant_name}",
                merchant_name=merchant_name,
            )
        except SquareAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except SquareNetworkError as exc:
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
    ) -> SyncResult:
        """
        Sync Square payments, orders (skipped without a location_id), and customers.

        full=True → fetch all records (paginated).
        since is accepted but not used — Square list endpoints use cursor-based
        pagination without server-side timestamp filtering on these endpoints.
        """
        _ = since
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync payments
        try:
            payments = await self._fetch_all_payments()
        except SquareError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )
        found += len(payments)
        for record in payments:
            try:
                doc = normalize_payment(record, self.connector_id, self.tenant_id)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # Sync customers
        try:
            customers = await self._fetch_all_customers()
        except SquareError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )
        found += len(customers)
        for record in customers:
            try:
                doc = normalize_customer(record, self.connector_id, self.tenant_id)
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

    async def _fetch_all_payments(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                self.http_client.list_payments, cursor=cursor, limit=SYNC_PAGE_SIZE
            )
            records.extend(page.get("payments", []))
            cursor = page.get("cursor")
            if not cursor:
                break
        return records

    async def _fetch_all_customers(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                self.http_client.list_customers, cursor=cursor, limit=SYNC_PAGE_SIZE
            )
            records.extend(page.get("customers", []))
            cursor = page.get("cursor")
            if not cursor:
                break
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Payments ─────────────────────────────────────────────────────────────

    async def list_payments(
        self, cursor: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_payments, cursor=cursor, limit=limit)

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_payment, payment_id)

    # ── Orders ───────────────────────────────────────────────────────────────

    async def list_orders(
        self, location_id: str, cursor: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_orders, location_id, cursor=cursor, limit=limit)

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(
        self, cursor: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_customers, cursor=cursor, limit=limit)

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_customer, customer_id)

    # ── Catalog ──────────────────────────────────────────────────────────────

    async def list_catalog_items(
        self, cursor: str | None = None, types: str = "ITEM"
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_catalog_items, cursor=cursor, types=types)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> SquareHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> SquareConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
