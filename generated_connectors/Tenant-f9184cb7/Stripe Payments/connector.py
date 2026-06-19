from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from client import StripeHTTPClient
from exceptions import StripeAuthError, StripeError, StripeInvalidKeyError, StripeNetworkError
from helpers import CircuitBreaker, normalize_customer, normalize_event, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

STRIPE_BASE_URL = "https://api.stripe.com/v1"
SYNC_PAGE_SIZE = 100
CIRCUIT_BREAKER_THRESHOLD = 5


class StripeConnector:
    """
    Shielva connector for Stripe Payments.

    Provides authentication, health checks, full/incremental sync, and
    direct access to all major Stripe API resources.
    """

    connector_id: str = ""

    def __init__(
        self,
        api_key: str = "",
        connector_id: str = "",
        tenant_id: str = "",
    ) -> None:
        self._api_key = api_key
        self.connector_id = connector_id
        self._tenant_id = tenant_id
        self.http_client: StripeHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> StripeHTTPClient:
        return StripeHTTPClient(api_key=self._api_key)

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the API key by calling /v1/account."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_account)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or data.get("id", ""),
                message=f"Connected to Stripe account {data.get('id', '')}",
            )
        except StripeAuthError as exc:
            await client.aclose()
            code = getattr(exc, "code", "")
            msg = str(exc)
            if "Invalid Stripe API key" in msg or code in ("api_key_expired", "invalid_api_key"):
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Invalid Stripe API key: {msg}",
                )
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=msg,
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping Stripe /v1/account and return current health."""
        if not self._api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_account)
            await client.aclose()
            self._circuit_breaker.on_success()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Stripe API is reachable",
            )
        except StripeAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except StripeNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = ConnectorHealth.DEGRADED if not self._circuit_breaker.is_open else ConnectorHealth.OFFLINE
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
        Sync Stripe events into the knowledge base.

        full=True → fetch all events (paginated).
        since=<datetime> → fetch events created after that timestamp.
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        kwargs: dict[str, Any] = {"limit": SYNC_PAGE_SIZE}
        if not full and since:
            kwargs["created[gte]"] = int(since.timestamp())

        found = 0
        synced = 0
        failed = 0
        starting_after: str | None = None

        while True:
            page_kwargs = {**kwargs}
            if starting_after:
                page_kwargs["starting_after"] = starting_after
            try:
                page = await with_retry(self.http_client.list_events, **page_kwargs)
            except StripeError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            events: list[dict[str, Any]] = page.get("data", [])
            found += len(events)

            for event in events:
                try:
                    doc = normalize_event(event, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            if not page.get("has_more") or not events:
                break
            starting_after = events[-1]["id"]

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

    # ── Balance ──────────────────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_balance, self._api_key)

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(self, limit: int = 100, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_customers, limit=limit, **kwargs)

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_customer, customer_id)

    async def create_customer(self, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.create_customer, **kwargs)

    async def update_customer(self, customer_id: str, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.update_customer, customer_id, **kwargs)

    async def delete_customer(self, customer_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.delete_customer, customer_id)

    # ── Charges ──────────────────────────────────────────────────────────────

    async def list_charges(self, limit: int = 100, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_charges, limit=limit, **kwargs)

    async def get_charge(self, charge_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_charge, charge_id)

    # ── Payment Intents ───────────────────────────────────────────────────────

    async def list_payment_intents(self, limit: int = 100, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_payment_intents, limit=limit, **kwargs)

    async def get_payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_payment_intent, payment_intent_id)

    async def create_payment_intent(self, amount: int, currency: str, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.create_payment_intent, amount, currency, **kwargs)

    # ── Subscriptions ────────────────────────────────────────────────────────

    async def list_subscriptions(self, limit: int = 100, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_subscriptions, limit=limit, **kwargs)

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_subscription, subscription_id)

    async def cancel_subscription(self, subscription_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.cancel_subscription, subscription_id)

    # ── Products ─────────────────────────────────────────────────────────────

    async def list_products(self, limit: int = 100, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_products, limit=limit, **kwargs)

    # ── Invoices ─────────────────────────────────────────────────────────────

    async def list_invoices(self, limit: int = 100, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_invoices, limit=limit, **kwargs)

    # ── Refunds ──────────────────────────────────────────────────────────────

    async def create_refund(self, charge_id: str, amount: int | None = None, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.create_refund, charge_id, amount=amount, **kwargs)

    # ── Events ───────────────────────────────────────────────────────────────

    async def list_events(self, limit: int = 100, **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_events, limit=limit, **kwargs)

    async def get_event(self, event_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_event, event_id)

    # ── Webhooks ─────────────────────────────────────────────────────────────

    async def list_webhooks(self) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_webhooks)

    async def create_webhook(self, url: str, enabled_events: list[str], **kwargs: Any) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.create_webhook, url, enabled_events, **kwargs)

    async def delete_webhook(self, webhook_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.delete_webhook, webhook_id)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> StripeHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> StripeConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
