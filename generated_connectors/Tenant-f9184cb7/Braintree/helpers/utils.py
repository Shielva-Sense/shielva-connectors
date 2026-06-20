from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from exceptions import BraintreeAuthError, BraintreeError, BraintreeRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


# ── Retry helper ─────────────────────────────────────────────────────────────


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: BraintreeError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except BraintreeAuthError:
            raise
        except BraintreeRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except BraintreeError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Stable ID helper ─────────────────────────────────────────────────────────


def _stable_id(prefix: str, raw_id: str) -> str:
    """Return a 16-char hex digest for use as a stable source_id."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_transaction(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Braintree transaction dict to a ConnectorDocument."""
    txn_id: str = str(raw.get("id", ""))
    amount: str = str(raw.get("amount", ""))
    status: str = str(raw.get("status", ""))
    currency: str = str(raw.get("currencyIsoCode", raw.get("currency_iso_code", "")))
    created_at: str = str(raw.get("createdAt", raw.get("created_at", "")))
    title = f"Transaction {txn_id} — {currency} {amount} ({status})"
    return ConnectorDocument(
        source_id=_stable_id("transaction", txn_id),
        title=title,
        content=json.dumps(raw, default=str),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "type": "transaction",
            "transaction_id": txn_id,
            "amount": amount,
            "status": status,
            "currency": currency,
            "created_at": created_at,
        },
    )


def normalize_customer(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Braintree customer dict to a ConnectorDocument."""
    cust_id: str = str(raw.get("id", ""))
    first_name: str = raw.get("firstName", raw.get("first_name", ""))
    last_name: str = raw.get("lastName", raw.get("last_name", ""))
    email: str = raw.get("email", "")
    company: str = raw.get("company", "")
    full_name = f"{first_name} {last_name}".strip() or company or cust_id
    title = f"Customer {full_name}"
    return ConnectorDocument(
        source_id=_stable_id("customer", cust_id),
        title=title,
        content=json.dumps(raw, default=str),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "type": "customer",
            "customer_id": cust_id,
            "email": email,
            "company": company,
            "first_name": first_name,
            "last_name": last_name,
        },
    )


def normalize_subscription(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Braintree subscription dict to a ConnectorDocument."""
    sub_id: str = str(raw.get("id", ""))
    plan_id: str = str(raw.get("planId", raw.get("plan_id", "")))
    status: str = str(raw.get("status", ""))
    price: str = str(raw.get("price", ""))
    title = f"Subscription {sub_id} — Plan {plan_id} ({status})"
    return ConnectorDocument(
        source_id=_stable_id("subscription", sub_id),
        title=title,
        content=json.dumps(raw, default=str),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "type": "subscription",
            "subscription_id": sub_id,
            "plan_id": plan_id,
            "status": status,
            "price": price,
        },
    )


def normalize_plan(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Braintree plan dict to a ConnectorDocument."""
    plan_id: str = str(raw.get("id", ""))
    name: str = str(raw.get("name", plan_id))
    price: str = str(raw.get("price", ""))
    billing_frequency: Any = raw.get("billingFrequency", raw.get("billing_frequency", ""))
    title = f"Plan {name} — {price} / {billing_frequency} mo"
    return ConnectorDocument(
        source_id=_stable_id("plan", plan_id),
        title=title,
        content=json.dumps(raw, default=str),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "type": "plan",
            "plan_id": plan_id,
            "name": name,
            "price": price,
            "billing_frequency": billing_frequency,
        },
    )
