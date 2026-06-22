from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from client import PayPalHTTPClient
from exceptions import (
    PayPalAuthError,
    PayPalError,
    PayPalInvalidCredentialsError,
    PayPalNetworkError,
)
from helpers import CircuitBreaker, normalize_order, normalize_transaction, with_retry
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

SYNC_PAGE_SIZE = 100
CIRCUIT_BREAKER_THRESHOLD = 5


class PayPalConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for PayPal REST API v2.

    Provides OAuth2 client_credentials authentication, health checks,
    full/incremental sync of transactions and orders, and direct access
    to key PayPal API resources.
    """

    CONNECTOR_TYPE: str = "paypal"
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
            self._tenant_id = tenant_id

        # PayPal-specific attrs
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._sandbox: bool = str(_config.get("sandbox", "")).lower() == "true"
        self.http_client: PayPalHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> PayPalHTTPClient:
        return PayPalHTTPClient(
            client_id=self._client_id,
            client_secret=self._client_secret,
            sandbox=self._sandbox,
        )

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate client_id/client_secret by acquiring an OAuth2 token."""
        if not self._client_id or not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_token)
            await client.aclose()
            self.http_client = self._make_client()
            mode = "sandbox" if self._sandbox else "live"
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or f"paypal_{mode}",
                message=f"Connected to PayPal ({mode} mode)",
            )
        except PayPalInvalidCredentialsError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid PayPal credentials: {exc.message}",
            )
        except PayPalAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """POST /v1/oauth2/token to verify credentials are still valid."""
        if not self._client_id or not self._client_secret:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_token)
            await client.aclose()
            self._circuit_breaker.on_success()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="PayPal API is reachable",
            )
        except PayPalAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except PayPalNetworkError as exc:
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
        Sync PayPal transactions and orders into the knowledge base.

        full=True → fetches the last 31 days of transactions.
        since=<datetime> → fetches transactions after that timestamp.
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        now = datetime.now(timezone.utc)
        if full or since is None:
            from datetime import timedelta
            start_dt = now - timedelta(days=31)
        else:
            start_dt = since

        # PayPal date format: ISO 8601 with seconds + Z
        start_date = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_date = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        found = 0
        synced = 0
        failed = 0

        # Sync transactions (paginated)
        page = 1
        while True:
            try:
                data = await with_retry(
                    self.http_client.list_transactions,
                    start_date=start_date,
                    end_date=end_date,
                    page=page,
                    page_size=SYNC_PAGE_SIZE,
                )
            except PayPalError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            rows: list[dict[str, Any]] = data.get("transaction_details", [])
            found += len(rows)

            for row in rows:
                try:
                    doc = normalize_transaction(
                        row, self.connector_id, self._tenant_id, self._sandbox
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            total_pages: int = int(data.get("total_pages", 1))
            if page >= total_pages:
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

    # ── Transactions ─────────────────────────────────────────────────────────

    async def list_transactions(
        self,
        start_date: str,
        end_date: str,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """GET /v1/reporting/transactions — paginated transaction list."""
        client = self._ensure_client()
        return await with_retry(
            client.list_transactions,
            start_date=start_date,
            end_date=end_date,
            page=page,
            page_size=page_size,
        )

    # ── Orders ───────────────────────────────────────────────────────────────

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """GET /v2/checkout/orders/{order_id}"""
        client = self._ensure_client()
        return await with_retry(client.get_order, order_id)

    # ── Payments ─────────────────────────────────────────────────────────────

    async def list_payments(self, page_size: int = 20, page: int = 1) -> dict[str, Any]:
        """GET /v1/payments/payment — paginated payment list."""
        client = self._ensure_client()
        return await with_retry(client.list_payments, page_size=page_size, page=page)

    # ── Balance ──────────────────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """GET /v1/reporting/balances"""
        client = self._ensure_client()
        return await with_retry(client.get_balance)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> PayPalHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> PayPalConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
