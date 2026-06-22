from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import MagentoAuthError, MagentoError, MagentoRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(raw_id: int | str) -> str:
    """Return the first 16 hex chars of SHA-256(str(raw_id))."""
    return hashlib.sha256(str(raw_id).encode()).hexdigest()[:16]


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
    last_exc: MagentoError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except MagentoAuthError:
            raise
        except MagentoRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except MagentoError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_order(
    order: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    base_url: str,
) -> ConnectorDocument:
    """Convert a raw Magento Order object into a ConnectorDocument."""
    order_id = order.get("entity_id", 0) or order.get("id", 0)
    increment_id = order.get("increment_id", str(order_id))
    status = order.get("status", "")
    grand_total = order.get("grand_total", 0)
    customer_firstname = order.get("customer_firstname", "")
    customer_lastname = order.get("customer_lastname", "")
    customer_email = order.get("customer_email", "")
    items_count = order.get("items_qty_ordered") or order.get("total_item_count", 0)
    created_at = order.get("created_at", "")

    customer_name = f"{customer_firstname} {customer_lastname}".strip() or customer_email or "Guest"

    items: list[dict[str, Any]] = order.get("items", [])
    item_lines = [
        f"  - {item.get('name', '')} x{item.get('qty_ordered', 1)} @ {item.get('price', 0)}"
        for item in items
    ]

    content_parts = [
        f"Order #{increment_id}",
        f"Customer: {customer_name}",
        f"Email: {customer_email}",
        f"Status: {status}",
        f"Grand total: {grand_total}",
        f"Items count: {items_count}",
        f"Created at: {created_at}",
    ]
    if item_lines:
        content_parts.append("Items:")
        content_parts.extend(item_lines)

    host = base_url.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"

    return ConnectorDocument(
        source_id=_stable_id(order_id),
        title=f"Order #{increment_id}: {customer_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{host}/admin/sales/order/view/order_id/{order_id}",
        metadata={
            "order_id": order_id,
            "increment_id": increment_id,
            "status": status,
            "grand_total": grand_total,
            "customer_email": customer_email,
            "items_count": items_count,
            "created_at": created_at,
        },
    )


def normalize_product(
    product: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    base_url: str,
) -> ConnectorDocument:
    """Convert a raw Magento Product object into a ConnectorDocument."""
    product_id = product.get("id", 0)
    sku = product.get("sku", "")
    name = product.get("name", "Untitled Product")
    type_id = product.get("type_id", "")
    status = product.get("status", 1)
    price = product.get("price", 0)
    visibility = product.get("visibility", 4)
    created_at = product.get("created_at", "")

    status_label = "enabled" if status == 1 else "disabled"
    visibility_map = {1: "not visible", 2: "catalog", 3: "search", 4: "catalog+search"}
    visibility_label = visibility_map.get(visibility, str(visibility))

    content_parts = [
        f"Product: {name}",
        f"SKU: {sku}",
        f"Type: {type_id}",
        f"Status: {status_label}",
        f"Price: {price}",
        f"Visibility: {visibility_label}",
        f"Created at: {created_at}",
    ]

    host = base_url.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"

    return ConnectorDocument(
        source_id=_stable_id(sku),
        title=f"{name} ({type_id})" if type_id else name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{host}/admin/catalog/product/edit/id/{product_id}",
        metadata={
            "sku": sku,
            "type_id": type_id,
            "status": status,
            "price": price,
            "visibility": visibility,
            "created_at": created_at,
        },
    )


def normalize_customer(
    customer: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    base_url: str,
) -> ConnectorDocument:
    """Convert a raw Magento Customer object into a ConnectorDocument."""
    customer_id = customer.get("id", 0)
    firstname = customer.get("firstname", "")
    lastname = customer.get("lastname", "")
    email = customer.get("email", "")
    created_at = customer.get("created_at", "")
    group_id = customer.get("group_id", 0)

    full_name = f"{firstname} {lastname}".strip() or email

    content_parts = [
        f"Customer: {full_name}",
        f"Email: {email}",
        f"Group ID: {group_id}",
        f"Created at: {created_at}",
    ]

    host = base_url.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"

    return ConnectorDocument(
        source_id=_stable_id(customer_id),
        title=f"Customer: {full_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{host}/admin/customer/index/edit/id/{customer_id}",
        metadata={
            "customer_id": customer_id,
            "email": email,
            "created_at": created_at,
            "group_id": group_id,
        },
    )
