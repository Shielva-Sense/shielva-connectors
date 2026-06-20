from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ChargebeeAuthError, ChargebeeError, ChargebeeRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(value: str) -> str:
    """Return a 16-character hex digest (sha256 prefix) for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_subscription(
    subscription: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    site: str,
) -> ConnectorDocument:
    """Convert a raw Chargebee subscription object into a ConnectorDocument.

    Chargebee wraps each item in the list response under a ``"subscription"``
    key: ``{"subscription": {...}, "customer": {...}}``. This function accepts
    either the unwrapped subscription dict or the full wrapper — it unwraps
    automatically.
    """
    # Unwrap Chargebee list-item envelope if present
    if "subscription" in subscription:
        subscription = subscription["subscription"]

    sub_id: str = subscription.get("id", "")
    plan_id: str = subscription.get("plan_id", "") or subscription.get("subscription_items", [{}])[0].get("item_price_id", "") if subscription.get("subscription_items") else subscription.get("plan_id", "")
    status: str = subscription.get("status", "")
    customer_id: str = subscription.get("customer_id", "")
    current_term_start: Any = subscription.get("current_term_start", None)
    current_term_end: Any = subscription.get("current_term_end", None)
    created_at: Any = subscription.get("created_at", None)
    updated_at: Any = subscription.get("updated_at", None)
    currency_code: str = subscription.get("currency_code", "")
    mrr: Any = subscription.get("mrr", None)
    plan_amount: Any = subscription.get("plan_amount", None)
    billing_period: Any = subscription.get("billing_period", None)
    billing_period_unit: str = subscription.get("billing_period_unit", "")

    content_parts: list[str] = [f"Subscription ID: {sub_id}"]
    if plan_id:
        content_parts.append(f"Plan: {plan_id}")
    if status:
        content_parts.append(f"Status: {status}")
    if customer_id:
        content_parts.append(f"Customer ID: {customer_id}")
    if currency_code:
        content_parts.append(f"Currency: {currency_code}")
    if mrr is not None:
        content_parts.append(f"MRR: {mrr}")
    if plan_amount is not None:
        content_parts.append(f"Plan Amount: {plan_amount}")
    if billing_period is not None and billing_period_unit:
        content_parts.append(f"Billing: every {billing_period} {billing_period_unit}(s)")
    if current_term_start is not None:
        content_parts.append(f"Term Start: {current_term_start}")
    if current_term_end is not None:
        content_parts.append(f"Term End: {current_term_end}")
    if created_at is not None:
        content_parts.append(f"Created At: {created_at}")

    source_id = _short_id(f"subscription:{sub_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Subscription {sub_id}: {plan_id or status}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://{site}.chargebee.com/subscriptions/{sub_id}",
        metadata={
            "subscription_id": sub_id,
            "plan_id": plan_id,
            "status": status,
            "customer_id": customer_id,
            "currency_code": currency_code,
            "mrr": mrr,
            "plan_amount": plan_amount,
            "billing_period": billing_period,
            "billing_period_unit": billing_period_unit,
            "current_term_start": current_term_start,
            "current_term_end": current_term_end,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_customer(
    customer: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    site: str,
) -> ConnectorDocument:
    """Convert a raw Chargebee customer object into a ConnectorDocument.

    Unwraps the Chargebee list-item envelope (``{"customer": {...}}``) if present.
    """
    if "customer" in customer:
        customer = customer["customer"]

    customer_id: str = customer.get("id", "")
    first_name: str = customer.get("first_name", "") or ""
    last_name: str = customer.get("last_name", "") or ""
    email: str = customer.get("email", "") or ""
    company: str = customer.get("company", "") or ""
    phone: str = customer.get("phone", "") or ""
    created_at: Any = customer.get("created_at", None)
    updated_at: Any = customer.get("updated_at", None)
    taxability: str = customer.get("taxability", "") or ""
    auto_collection: str = customer.get("auto_collection", "") or ""

    name = f"{first_name} {last_name}".strip() or customer_id

    content_parts: list[str] = [f"Customer ID: {customer_id}"]
    if name and name != customer_id:
        content_parts.append(f"Name: {name}")
    if email:
        content_parts.append(f"Email: {email}")
    if company:
        content_parts.append(f"Company: {company}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if taxability:
        content_parts.append(f"Taxability: {taxability}")
    if auto_collection:
        content_parts.append(f"Auto Collection: {auto_collection}")
    if created_at is not None:
        content_parts.append(f"Created At: {created_at}")

    source_id = _short_id(f"customer:{customer_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Customer: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://{site}.chargebee.com/customers/{customer_id}",
        metadata={
            "customer_id": customer_id,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "company": company,
            "phone": phone,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_invoice(
    invoice: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    site: str,
) -> ConnectorDocument:
    """Convert a raw Chargebee invoice object into a ConnectorDocument.

    Unwraps the Chargebee list-item envelope (``{"invoice": {...}}``) if present.
    """
    if "invoice" in invoice:
        invoice = invoice["invoice"]

    invoice_id: str = invoice.get("id", "")
    customer_id: str = invoice.get("customer_id", "") or ""
    subscription_id: str = invoice.get("subscription_id", "") or ""
    status: str = invoice.get("status", "") or ""
    amount_due: Any = invoice.get("amount_due", None)
    amount_paid: Any = invoice.get("amount_paid", None)
    total: Any = invoice.get("total", None)
    currency_code: str = invoice.get("currency_code", "") or ""
    date: Any = invoice.get("date", None)
    due_date: Any = invoice.get("due_date", None)
    paid_at: Any = invoice.get("paid_at", None)
    created_at: Any = invoice.get("date", None)  # Chargebee uses "date" as creation timestamp

    content_parts: list[str] = [f"Invoice ID: {invoice_id}"]
    if customer_id:
        content_parts.append(f"Customer ID: {customer_id}")
    if subscription_id:
        content_parts.append(f"Subscription ID: {subscription_id}")
    if status:
        content_parts.append(f"Status: {status}")
    if total is not None:
        content_parts.append(f"Total: {total}")
    if amount_due is not None:
        content_parts.append(f"Amount Due: {amount_due}")
    if amount_paid is not None:
        content_parts.append(f"Amount Paid: {amount_paid}")
    if currency_code:
        content_parts.append(f"Currency: {currency_code}")
    if date is not None:
        content_parts.append(f"Invoice Date: {date}")
    if due_date is not None:
        content_parts.append(f"Due Date: {due_date}")
    if paid_at is not None:
        content_parts.append(f"Paid At: {paid_at}")

    source_id = _short_id(f"invoice:{invoice_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Invoice {invoice_id}: {status}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://{site}.chargebee.com/invoices/{invoice_id}",
        metadata={
            "invoice_id": invoice_id,
            "customer_id": customer_id,
            "subscription_id": subscription_id,
            "status": status,
            "total": total,
            "amount_due": amount_due,
            "amount_paid": amount_paid,
            "currency_code": currency_code,
            "date": date,
            "due_date": due_date,
            "paid_at": paid_at,
            "created_at": created_at,
        },
    )


# ── Retry helper ──────────────────────────────────────────────────────────────


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
    last_exc: ChargebeeError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ChargebeeAuthError:
            raise  # no retry on auth failures
        except ChargebeeRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ChargebeeError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
