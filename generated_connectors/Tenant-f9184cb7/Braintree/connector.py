from __future__ import annotations

from typing import Any

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
            self.config: dict[str, Any] = config or {}

from client import BraintreeHTTPClient
from exceptions import BraintreeAuthError, BraintreeError, BraintreeNetworkError
from helpers import (
    normalize_customer,
    normalize_plan,
    normalize_subscription,
    normalize_transaction,
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

CONNECTOR_TYPE = "braintree"
AUTH_TYPE = "api_key"

_REQUIRED_FIELDS = ("merchant_id", "public_key", "private_key", "environment")


class BraintreeConnector(BaseConnector):
    """Shielva connector for Braintree (PayPal) payment gateway.

    Syncs transactions, customers, subscriptions, plans, and disputes.
    Authentication uses HTTP Basic Auth with Public Key / Private Key,
    plus a Merchant ID to scope all API calls.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=config)
        self.client = BraintreeHTTPClient(config=self.config)

    # ── Install & health ─────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling the merchant endpoint."""
        missing = [f for f in _REQUIRED_FIELDS if not self.config.get(f)]
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = BraintreeHTTPClient(config=self.config)
        try:
            data = await with_retry(client.get_merchant)
            await client.aclose()
            merchant_id: str = self.config.get("merchant_id", "")
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or merchant_id,
                message=f"Connected to Braintree merchant {data.get('id', merchant_id)}",
            )
        except BraintreeAuthError as exc:
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

    async def health_check(self) -> HealthCheckResult:
        """Ping the merchant endpoint and return current health."""
        missing = [f for f in _REQUIRED_FIELDS if not self.config.get(f)]
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = BraintreeHTTPClient(config=self.config)
        try:
            await with_retry(client.get_merchant)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Braintree API is reachable",
            )
        except BraintreeAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except BraintreeNetworkError as exc:
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

    async def sync(self, kb_id: str = "", **kwargs: Any) -> SyncResult:
        """Full sync of all resources: transactions, customers, subscriptions, plans."""
        found = 0
        synced = 0
        failed = 0

        resources: list[tuple[str, Any, Any]] = [
            ("transactions", self.list_transactions, normalize_transaction),
            ("customers", self.list_customers, normalize_customer),
            ("subscriptions", self.list_subscriptions, normalize_subscription),
            ("plans", self.list_plans, normalize_plan),
        ]

        for resource_name, list_fn, normalizer in resources:
            try:
                items: list[dict[str, Any]] = await list_fn()
                found += len(items)
                for item in items:
                    try:
                        doc: ConnectorDocument = normalizer(
                            item,
                            connector_id=self.connector_id,
                            tenant_id=self.tenant_id,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except BraintreeError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=f"Failed to sync {resource_name}: {exc}",
                )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Transactions ─────────────────────────────────────────────────────────

    async def list_transactions(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all transactions via paginated advanced search."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            raw = await with_retry(self.client.search_transactions, page=page)
            items: list[dict[str, Any]] = raw.get("creditCardTransactions", raw.get("transactions", []))
            if not items:
                break
            results.extend(items)
            total_pages: int = int(raw.get("totalPages", raw.get("total_pages", 1)))
            if page >= total_pages:
                break
            page += 1
        return results

    async def get_transaction(self, transaction_id: str) -> dict[str, Any]:
        """Fetch a single transaction by ID."""
        return await with_retry(self.client.get_transaction, transaction_id)

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(self) -> list[dict[str, Any]]:
        """Return all customers via paginated advanced search."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            raw = await with_retry(self.client.get_customers, page=page)
            items: list[dict[str, Any]] = raw.get("customers", [])
            if not items:
                break
            results.extend(items)
            total_pages: int = int(raw.get("totalPages", raw.get("total_pages", 1)))
            if page >= total_pages:
                break
            page += 1
        return results

    # ── Subscriptions ────────────────────────────────────────────────────────

    async def list_subscriptions(self) -> list[dict[str, Any]]:
        """Return all subscriptions via paginated advanced search."""
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            raw = await with_retry(self.client.get_subscriptions, page=page)
            items: list[dict[str, Any]] = raw.get("subscriptions", [])
            if not items:
                break
            results.extend(items)
            total_pages: int = int(raw.get("totalPages", raw.get("total_pages", 1)))
            if page >= total_pages:
                break
            page += 1
        return results

    # ── Plans ────────────────────────────────────────────────────────────────

    async def list_plans(self) -> list[dict[str, Any]]:
        """Return all billing plans."""
        raw = await with_retry(self.client.get_plans)
        plans: list[dict[str, Any]] = raw.get("plans", [])
        return plans

    # ── Context manager ──────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> BraintreeConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
