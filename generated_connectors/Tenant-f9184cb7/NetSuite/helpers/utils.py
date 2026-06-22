from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import NetSuiteAuthError, NetSuiteError, NetSuiteRateLimitError
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
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: NetSuiteError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except NetSuiteAuthError:
            raise
        except NetSuiteRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except NetSuiteError as exc:
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


def _stable_id(entity_type: str, internal_id: str) -> str:
    """SHA-256(entity_type:internal_id)[:16] — stable document id."""
    raw = f"{entity_type}:{internal_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_customer(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw NetSuite customer record into a ConnectorDocument.

    NetSuite REST returns customer records with fields like:
    id, companyName, email, phone, entityId, balance, isInactive.
    """
    internal_id = str(raw.get("id", ""))
    company_name = (
        raw.get("companyName")
        or raw.get("entityId")
        or f"Customer {internal_id}"
    )
    email = raw.get("email", "")
    phone = raw.get("phone", "")
    balance = raw.get("balance", 0)
    is_inactive = raw.get("isInactive", False)
    entity_id = raw.get("entityId", "")
    subsidiary = raw.get("subsidiary", {})
    subsidiary_name = (
        subsidiary.get("refName", "")
        if isinstance(subsidiary, dict)
        else ""
    )

    content_parts = [f"Customer: {company_name}"]
    if entity_id and entity_id != company_name:
        content_parts.append(f"Entity ID: {entity_id}")
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    content_parts.append(f"Balance: {balance}")
    content_parts.append(f"Active: {not is_inactive}")
    if subsidiary_name:
        content_parts.append(f"Subsidiary: {subsidiary_name}")

    return ConnectorDocument(
        source_id=_stable_id("customer", internal_id),
        title=f"Customer: {company_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "customer",
            "internal_id": internal_id,
            "entity_id": entity_id,
            "company_name": company_name,
            "email": email,
            "phone": phone,
            "balance": balance,
            "is_inactive": is_inactive,
            "subsidiary": subsidiary_name,
        },
    )


def normalize_invoice(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw NetSuite invoice (transaction) record into a ConnectorDocument.

    NetSuite REST returns invoice records with fields like:
    id, tranId, entity, tranDate, dueDate, total, amountRemaining, status.
    """
    internal_id = str(raw.get("id", ""))
    tran_id = raw.get("tranId", internal_id)

    entity = raw.get("entity", {})
    customer_name = (
        entity.get("refName", "")
        if isinstance(entity, dict)
        else str(entity)
    )

    tran_date = raw.get("tranDate", "")
    due_date = raw.get("dueDate", "")
    total = raw.get("total", 0)
    amount_remaining = raw.get("amountRemaining", 0)

    status = raw.get("status", {})
    status_name = (
        status.get("refName", "")
        if isinstance(status, dict)
        else str(status)
    )

    subsidiary = raw.get("subsidiary", {})
    subsidiary_name = (
        subsidiary.get("refName", "")
        if isinstance(subsidiary, dict)
        else ""
    )

    content_parts = [f"Invoice #{tran_id}"]
    if customer_name:
        content_parts.append(f"Customer: {customer_name}")
    content_parts.append(f"Total: {total}")
    content_parts.append(f"Amount Remaining: {amount_remaining}")
    if tran_date:
        content_parts.append(f"Transaction Date: {tran_date}")
    if due_date:
        content_parts.append(f"Due Date: {due_date}")
    if status_name:
        content_parts.append(f"Status: {status_name}")
    if subsidiary_name:
        content_parts.append(f"Subsidiary: {subsidiary_name}")

    return ConnectorDocument(
        source_id=_stable_id("invoice", internal_id),
        title=f"Invoice #{tran_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "invoice",
            "internal_id": internal_id,
            "tran_id": tran_id,
            "customer_name": customer_name,
            "total": total,
            "amount_remaining": amount_remaining,
            "tran_date": tran_date,
            "due_date": due_date,
            "status": status_name,
            "subsidiary": subsidiary_name,
        },
    )


def normalize_item(
    raw: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw NetSuite item record into a ConnectorDocument.

    NetSuite REST returns item records with fields like:
    id, itemId, displayName, description, salesPrice, itemType, isInactive.
    """
    internal_id = str(raw.get("id", ""))
    item_id = raw.get("itemId", f"Item {internal_id}")
    display_name = raw.get("displayName", item_id)
    description = raw.get("description", "")
    sales_price = raw.get("salesPrice", 0)
    item_type = raw.get("itemType", {})
    item_type_name = (
        item_type.get("refName", "")
        if isinstance(item_type, dict)
        else str(item_type)
    )
    is_inactive = raw.get("isInactive", False)

    content_parts = [f"Item: {display_name}"]
    if item_id != display_name:
        content_parts.append(f"Item ID: {item_id}")
    if description:
        content_parts.append(f"Description: {description}")
    if item_type_name:
        content_parts.append(f"Type: {item_type_name}")
    content_parts.append(f"Sales Price: {sales_price}")
    content_parts.append(f"Active: {not is_inactive}")

    return ConnectorDocument(
        source_id=_stable_id("item", internal_id),
        title=f"Item: {display_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "item",
            "internal_id": internal_id,
            "item_id": item_id,
            "display_name": display_name,
            "description": description,
            "sales_price": sales_price,
            "item_type": item_type_name,
            "is_inactive": is_inactive,
        },
    )
