from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from exceptions import PayPalAuthError, PayPalError, PayPalRateLimitError

if TYPE_CHECKING:
    from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

PAYPAL_LIVE_DASHBOARD = "https://www.paypal.com/activity/payment"
PAYPAL_SANDBOX_DASHBOARD = "https://www.sandbox.paypal.com/activity/payment"


def _stable_id(prefix: str, raw_id: str) -> str:
    """Return a stable 16-char hex ID: SHA-256('{prefix}:{raw_id}')[:16]."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


def normalize_transaction(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    sandbox: bool = False,
) -> "ConnectorDocument":
    """Normalize a PayPal transaction detail row into a ConnectorDocument."""
    from models import ConnectorDocument

    info: dict[str, Any] = raw.get("transaction_info", raw)
    txn_id: str = info.get("transaction_id", "")
    amount_info: dict[str, Any] = info.get("transaction_amount", {})
    currency: str = amount_info.get("currency_code", "")
    value: str = amount_info.get("value", "0.00")
    status: str = info.get("transaction_status", "")
    initiation_date: str = info.get("transaction_initiation_date", "")
    event_code: str = info.get("transaction_event_code", "")

    payer_info: dict[str, Any] = raw.get("payer_info", {})
    payer_email: str = payer_info.get("email_address", "")
    payer_name_dict: dict[str, Any] = payer_info.get("payer_name", {})
    payer_name: str = (
        f"{payer_name_dict.get('given_name', '')} {payer_name_dict.get('surname', '')}".strip()
        or payer_email
    )

    title = f"PayPal Transaction {txn_id} — {currency} {value} ({status})"
    dashboard_base = PAYPAL_SANDBOX_DASHBOARD if sandbox else PAYPAL_LIVE_DASHBOARD
    source_url = f"{dashboard_base}/{txn_id}" if txn_id else ""

    content_parts = [
        f"Transaction ID: {txn_id}",
        f"Amount: {currency} {value}",
        f"Status: {status}",
        f"Date: {initiation_date}",
        f"Event Code: {event_code}",
    ]
    if payer_name:
        content_parts.append(f"Payer: {payer_name}")
    if payer_email:
        content_parts.append(f"Payer Email: {payer_email}")

    return ConnectorDocument(
        source_id=_stable_id("transaction", txn_id) if txn_id else _stable_id("transaction", json.dumps(raw)),
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "transaction_id": txn_id,
            "currency": currency,
            "amount": value,
            "status": status,
            "event_code": event_code,
            "initiation_date": initiation_date,
            "payer_email": payer_email,
            "payer_name": payer_name,
            "sandbox": sandbox,
        },
    )


def normalize_order(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    sandbox: bool = False,
) -> "ConnectorDocument":
    """Normalize a PayPal order (v2/checkout/orders) into a ConnectorDocument."""
    from models import ConnectorDocument

    order_id: str = raw.get("id", "")
    status: str = raw.get("status", "")
    create_time: str = raw.get("create_time", "")

    # Purchase units breakdown
    purchase_units: list[dict[str, Any]] = raw.get("purchase_units", [])
    first_unit: dict[str, Any] = purchase_units[0] if purchase_units else {}
    amount_info: dict[str, Any] = first_unit.get("amount", {})
    currency: str = amount_info.get("currency_code", "")
    value: str = amount_info.get("value", "0.00")
    description: str = first_unit.get("description", "")

    # Payer
    payer: dict[str, Any] = raw.get("payer", {})
    payer_email: str = payer.get("email_address", "")
    payer_name_dict: dict[str, Any] = payer.get("name", {})
    payer_name: str = (
        f"{payer_name_dict.get('given_name', '')} {payer_name_dict.get('surname', '')}".strip()
        or payer_email
    )

    title = f"PayPal Order {order_id} — {currency} {value} ({status})"
    dashboard_base = "https://www.sandbox.paypal.com" if sandbox else "https://www.paypal.com"
    source_url = f"{dashboard_base}/checkoutnow?token={order_id}" if order_id else ""

    content_parts = [
        f"Order ID: {order_id}",
        f"Status: {status}",
        f"Amount: {currency} {value}",
        f"Created: {create_time}",
    ]
    if description:
        content_parts.append(f"Description: {description}")
    if payer_name:
        content_parts.append(f"Payer: {payer_name}")
    if payer_email:
        content_parts.append(f"Payer Email: {payer_email}")

    return ConnectorDocument(
        source_id=_stable_id("order", order_id) if order_id else _stable_id("order", json.dumps(raw)),
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "order_id": order_id,
            "status": status,
            "currency": currency,
            "amount": value,
            "create_time": create_time,
            "description": description,
            "payer_email": payer_email,
            "payer_name": payer_name,
            "sandbox": sandbox,
            "purchase_units": len(purchase_units),
        },
    )


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: PayPalError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except PayPalAuthError:
            raise
        except PayPalRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except PayPalError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


class CircuitBreaker:
    """Simple three-state circuit breaker (closed → open → half-open → closed)."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self._failures: int = 0
        self._state: str = "closed"
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        if self._state == "open":
            import time
            if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                self._state = "half-open"
        return self._state

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            import time
            self._state = "open"
            self._opened_at = time.monotonic()

    def on_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    @property
    def is_open(self) -> bool:
        return self.state == "open"
