from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import SquareAuthError, SquareError, SquareRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: SquareError | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except SquareAuthError:
            raise
        except SquareRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except SquareError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_id(prefix: str, raw_id: str) -> str:
    """Return a 16-char hex SHA-256 fingerprint, e.g. stable_id('payment', payment_id)."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


def _cents_to_float(amount_money: dict[str, Any] | None) -> float:
    """Convert a Square Money object (amount in smallest unit) to a float."""
    if not amount_money:
        return 0.0
    amount = amount_money.get("amount", 0) or 0
    currency = amount_money.get("currency", "USD")
    # Square stores in smallest currency unit (cents for USD)
    # Standard: divide by 100 for currencies with 2 decimal places.
    # Keep it simple — assume 2 decimal places for now.
    _ = currency
    return round(int(amount) / 100, 2)


def normalize_payment(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw Square Payment object into a ConnectorDocument."""
    payment_id = record.get("id", "")
    stable = _stable_id("payment", payment_id)

    amount_money = record.get("amount_money") or {}
    total_float = _cents_to_float(amount_money)
    currency = amount_money.get("currency", "USD")

    status = record.get("status", "") or ""
    source_type = record.get("source_type", "") or ""
    location_id = record.get("location_id", "") or ""
    order_id = record.get("order_id", "") or ""
    created_at = record.get("created_at", "") or ""
    updated_at = record.get("updated_at", "") or ""
    receipt_url = record.get("receipt_url", "") or ""
    note = record.get("note", "") or ""

    title = f"Square payment: {payment_id} — {status} {total_float} {currency}"
    content_parts = [
        f"Payment ID: {payment_id}",
        f"Status: {status}",
        f"Amount: {total_float} {currency}",
        f"Source type: {source_type}",
        f"Location ID: {location_id}",
        f"Order ID: {order_id}",
        f"Created at: {created_at}",
        f"Updated at: {updated_at}",
        f"Note: {note}",
    ]

    return ConnectorDocument(
        source_id=stable,
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=receipt_url,
        metadata={
            "object_type": "payment",
            "payment_id": payment_id,
            "status": status,
            "amount": total_float,
            "currency": currency,
            "source_type": source_type,
            "location_id": location_id,
            "order_id": order_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_customer(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw Square Customer object into a ConnectorDocument."""
    customer_id = record.get("id", "")

    given_name = record.get("given_name", "") or ""
    family_name = record.get("family_name", "") or ""
    name = f"{given_name} {family_name}".strip() or "Unknown"
    email = record.get("email_address", "") or ""
    phone = record.get("phone_number", "") or ""
    reference_id = record.get("reference_id", "") or ""
    note = record.get("note", "") or ""
    created_at = record.get("created_at", "") or ""
    updated_at = record.get("updated_at", "") or ""

    title = f"Square customer: {name}" + (f" <{email}>" if email else "")
    content_parts = [
        f"Customer ID: {customer_id}",
        f"Name: {name}",
        f"Email: {email}",
        f"Phone: {phone}",
        f"Reference ID: {reference_id}",
        f"Note: {note}",
        f"Created at: {created_at}",
        f"Updated at: {updated_at}",
    ]

    return ConnectorDocument(
        source_id=customer_id,
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "customer",
            "customer_id": customer_id,
            "name": name,
            "email": email,
            "phone": phone,
            "reference_id": reference_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


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
            if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                self._state = "half-open"
        return self._state

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()

    def on_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    @property
    def is_open(self) -> bool:
        return self.state == "open"
