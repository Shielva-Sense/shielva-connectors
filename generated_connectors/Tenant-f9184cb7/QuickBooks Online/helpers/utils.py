from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from exceptions import QuickBooksAuthError, QuickBooksError, QuickBooksRateLimitError
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
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    skip_on: tuple[type[Exception], ...] | type[Exception] | None = None,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    ``skip_on`` is an optional exception type (or tuple) that is re-raised
    immediately without retrying, in addition to the built-in auth skip.
    """
    _skip: tuple[type[Exception], ...] = (QuickBooksAuthError,)
    if skip_on is not None:
        if isinstance(skip_on, tuple):
            _skip = _skip + skip_on
        else:
            _skip = _skip + (skip_on,)

    last_exc: QuickBooksError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except _skip:
            raise
        except QuickBooksRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except QuickBooksError as exc:
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


def _stable_id(entity_type: str, qbo_id: str) -> str:
    """SHA-256(entity_type:qbo_id)[:16] — stable document id."""
    raw = f"{entity_type}:{qbo_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _make_id(prefix: str, entity_id: str) -> str:
    """Public alias for _stable_id — SHA-256(prefix:entity_id)[:16]."""
    return hashlib.sha256(f"{prefix}:{entity_id}".encode()).hexdigest()[:16]


def normalize_customer(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw QBO Customer object into a ConnectorDocument."""
    qbo_id = str(raw.get("Id", ""))
    name = raw.get("DisplayName") or raw.get("FullyQualifiedName") or f"Customer {qbo_id}"
    email = raw.get("PrimaryEmailAddr", {}).get("Address", "") if isinstance(raw.get("PrimaryEmailAddr"), dict) else ""
    phone = raw.get("PrimaryPhone", {}).get("FreeFormNumber", "") if isinstance(raw.get("PrimaryPhone"), dict) else ""
    balance = raw.get("Balance", 0)
    active = raw.get("Active", True)

    content_parts = [f"Customer: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    content_parts.append(f"Balance: {balance}")
    content_parts.append(f"Active: {active}")

    return ConnectorDocument(
        source_id=_stable_id("Customer", qbo_id),
        title=f"Customer: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "Customer",
            "qbo_id": qbo_id,
            "email": email,
            "phone": phone,
            "balance": balance,
            "active": active,
        },
    )


def normalize_invoice(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw QBO Invoice object into a ConnectorDocument."""
    qbo_id = str(raw.get("Id", ""))
    doc_number = raw.get("DocNumber", qbo_id)
    customer_ref = raw.get("CustomerRef", {})
    customer_name = customer_ref.get("name", "") if isinstance(customer_ref, dict) else ""
    total = raw.get("TotalAmt", 0)
    balance = raw.get("Balance", 0)
    status = raw.get("EmailStatus", "")
    due_date = raw.get("DueDate", "")
    txn_date = raw.get("TxnDate", "")

    content_parts = [f"Invoice #{doc_number}"]
    if customer_name:
        content_parts.append(f"Customer: {customer_name}")
    content_parts.append(f"Total: {total}")
    content_parts.append(f"Balance Due: {balance}")
    if due_date:
        content_parts.append(f"Due Date: {due_date}")
    if txn_date:
        content_parts.append(f"Transaction Date: {txn_date}")

    return ConnectorDocument(
        source_id=_stable_id("Invoice", qbo_id),
        title=f"Invoice #{doc_number}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "Invoice",
            "qbo_id": qbo_id,
            "doc_number": doc_number,
            "customer_name": customer_name,
            "total": total,
            "balance": balance,
            "email_status": status,
            "due_date": due_date,
            "txn_date": txn_date,
        },
    )


def normalize_account(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw QBO Account object into a ConnectorDocument."""
    qbo_id = str(raw.get("Id", ""))
    name = raw.get("Name", f"Account {qbo_id}")
    account_type = raw.get("AccountType", "")
    account_sub_type = raw.get("AccountSubType", "")
    current_balance = raw.get("CurrentBalance", 0)
    active = raw.get("Active", True)
    classification = raw.get("Classification", "")

    content_parts = [f"Account: {name}"]
    if account_type:
        content_parts.append(f"Type: {account_type}")
    if account_sub_type:
        content_parts.append(f"Sub-type: {account_sub_type}")
    if classification:
        content_parts.append(f"Classification: {classification}")
    content_parts.append(f"Current Balance: {current_balance}")
    content_parts.append(f"Active: {active}")

    return ConnectorDocument(
        source_id=_stable_id("Account", qbo_id),
        title=f"Account: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "Account",
            "qbo_id": qbo_id,
            "account_type": account_type,
            "account_sub_type": account_sub_type,
            "current_balance": current_balance,
            "active": active,
            "classification": classification,
        },
    )


# ── Dict-returning normalizers (spec-compatible, no ConnectorDocument dep) ────


def normalize_customer_dict(customer: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw QBO Customer to a plain dict (spec-compatible form)."""
    return {
        "id": _make_id("customer", str(customer.get("Id", ""))),
        "source": "quickbooks",
        "type": "customer",
        "title": customer.get("DisplayName", customer.get("CompanyName", "")),
        "content": customer.get("PrintOnCheckName", ""),
        "metadata": {
            "customer_id": customer.get("Id"),
            "company_name": customer.get("CompanyName"),
            "email": customer.get("PrimaryEmailAddr", {}).get("Address")
            if isinstance(customer.get("PrimaryEmailAddr"), dict)
            else None,
            "phone": customer.get("PrimaryPhone", {}).get("FreeFormNumber")
            if isinstance(customer.get("PrimaryPhone"), dict)
            else None,
            "balance": customer.get("Balance"),
            "active": customer.get("Active"),
            "created_time": customer.get("MetaData", {}).get("CreateTime")
            if isinstance(customer.get("MetaData"), dict)
            else None,
        },
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def normalize_invoice_dict(invoice: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw QBO Invoice to a plain dict (spec-compatible form)."""
    customer_ref = invoice.get("CustomerRef", {}) if isinstance(invoice.get("CustomerRef"), dict) else {}
    return {
        "id": _make_id("invoice", str(invoice.get("Id", ""))),
        "source": "quickbooks",
        "type": "invoice",
        "title": f"Invoice #{invoice.get('DocNumber', '')}",
        "content": customer_ref.get("name", ""),
        "metadata": {
            "invoice_id": invoice.get("Id"),
            "doc_number": invoice.get("DocNumber"),
            "customer_ref": customer_ref.get("value"),
            "customer_name": customer_ref.get("name"),
            "total_amount": invoice.get("TotalAmt"),
            "balance": invoice.get("Balance"),
            "due_date": invoice.get("DueDate"),
            "txn_date": invoice.get("TxnDate"),
            "status": invoice.get("EmailStatus"),
        },
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def normalize_item_dict(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw QBO Item (product/service) to a plain dict."""
    return {
        "id": _make_id("item", str(item.get("Id", ""))),
        "source": "quickbooks",
        "type": "item",
        "title": item.get("Name", f"Item {item.get('Id', '')}"),
        "content": item.get("Description", ""),
        "metadata": {
            "item_id": item.get("Id"),
            "name": item.get("Name"),
            "description": item.get("Description"),
            "type": item.get("Type"),
            "unit_price": item.get("UnitPrice"),
            "purchase_cost": item.get("PurchaseCost"),
            "qty_on_hand": item.get("QtyOnHand"),
            "active": item.get("Active"),
            "taxable": item.get("Taxable"),
        },
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
