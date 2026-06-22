from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import XeroAuthError, XeroError, XeroRateLimitError

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(entity_type: str, xero_id: str) -> str:
    """Return a 16-char stable document ID: SHA-256(entity_type:xero_id)[:16]."""
    digest = hashlib.sha256(f"{entity_type}:{xero_id}".encode()).hexdigest()
    return digest[:16]


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
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: XeroError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except XeroAuthError:
            raise
        except XeroRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except XeroError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def normalize_invoice(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> Any:
    """Convert a raw Xero Invoice into a ConnectorDocument."""
    # Import here to avoid circular imports at module level
    from models import ConnectorDocument

    xero_id: str = raw.get("InvoiceID", "") or raw.get("InvoiceId", "")
    invoice_number: str = raw.get("InvoiceNumber", "")
    status: str = raw.get("Status", "")
    contact_name: str = (raw.get("Contact") or {}).get("Name", "")
    total: Any = raw.get("Total", 0)
    currency: str = raw.get("CurrencyCode", "")
    date_str: str = raw.get("DateString", "") or raw.get("Date", "")
    due_date_str: str = raw.get("DueDateString", "") or raw.get("DueDate", "")

    title = f"Invoice {invoice_number or xero_id}"
    if contact_name:
        title = f"{title} — {contact_name}"

    content_parts = [
        f"Invoice Number: {invoice_number}",
        f"Status: {status}",
        f"Contact: {contact_name}",
        f"Total: {total} {currency}",
        f"Date: {date_str}",
        f"Due Date: {due_date_str}",
    ]

    line_items: list[dict[str, Any]] = raw.get("LineItems") or []
    if line_items:
        content_parts.append("Line Items:")
        for item in line_items:
            desc = item.get("Description", "")
            qty = item.get("Quantity", "")
            unit = item.get("UnitAmount", "")
            line_total = item.get("LineAmount", "")
            content_parts.append(f"  - {desc} | Qty: {qty} | Unit: {unit} | Total: {line_total}")

    return ConnectorDocument(
        source_id=_stable_id("invoice", xero_id),
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://go.xero.com/AccountsReceivable/View.aspx?InvoiceID={xero_id}",
        metadata={
            "xero_id": xero_id,
            "invoice_number": invoice_number,
            "status": status,
            "contact_name": contact_name,
            "total": total,
            "currency": currency,
            "date": date_str,
            "due_date": due_date_str,
            "entity_type": "invoice",
        },
    )


def normalize_contact(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> Any:
    """Convert a raw Xero Contact into a ConnectorDocument."""
    from models import ConnectorDocument

    xero_id: str = raw.get("ContactID", "") or raw.get("ContactId", "")
    name: str = raw.get("Name", "")
    email: str = raw.get("EmailAddress", "")
    status: str = raw.get("ContactStatus", "")
    phone_numbers: list[dict[str, Any]] = raw.get("Phones") or []
    primary_phone = ""
    for ph in phone_numbers:
        if ph.get("PhoneType") == "DEFAULT" and ph.get("PhoneNumber"):
            primary_phone = ph["PhoneNumber"]
            break

    content_parts = [
        f"Name: {name}",
        f"Email: {email}",
        f"Status: {status}",
        f"Phone: {primary_phone}",
    ]
    addresses: list[dict[str, Any]] = raw.get("Addresses") or []
    for addr in addresses:
        if addr.get("AddressType") == "STREET":
            city = addr.get("City", "")
            country = addr.get("Country", "")
            if city or country:
                content_parts.append(f"Address: {city}, {country}")
            break

    return ConnectorDocument(
        source_id=_stable_id("contact", xero_id),
        title=f"Contact: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://go.xero.com/Contacts/View/{xero_id}",
        metadata={
            "xero_id": xero_id,
            "name": name,
            "email": email,
            "status": status,
            "phone": primary_phone,
            "entity_type": "contact",
        },
    )


def normalize_account(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> Any:
    """Convert a raw Xero Account into a ConnectorDocument."""
    from models import ConnectorDocument

    xero_id: str = raw.get("AccountID", "") or raw.get("AccountId", "")
    code: str = raw.get("Code", "")
    name: str = raw.get("Name", "")
    account_type: str = raw.get("Type", "")
    status: str = raw.get("Status", "")
    description: str = raw.get("Description", "")
    currency: str = raw.get("CurrencyCode", "")

    content_parts = [
        f"Code: {code}",
        f"Name: {name}",
        f"Type: {account_type}",
        f"Status: {status}",
        f"Currency: {currency}",
    ]
    if description:
        content_parts.append(f"Description: {description}")

    return ConnectorDocument(
        source_id=_stable_id("account", xero_id),
        title=f"Account: {code} — {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "xero_id": xero_id,
            "code": code,
            "name": name,
            "type": account_type,
            "status": status,
            "currency": currency,
            "entity_type": "account",
        },
    )
