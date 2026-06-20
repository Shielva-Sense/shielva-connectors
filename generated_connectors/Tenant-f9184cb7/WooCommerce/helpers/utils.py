from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import WooCommerceAuthError, WooCommerceError, WooCommerceRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(prefix: str, raw_id: int | str) -> str:
    """Return first 16 hex chars of SHA-256(f'{prefix}:{raw_id}').

    The prefix ensures product/order/customer IDs don't collide even when the
    numeric IDs are the same across resource types.
    """
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential back-off + jitter.

    Auth errors are never retried — they require human intervention.
    """
    last_exc: WooCommerceError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except WooCommerceAuthError:
            raise
        except WooCommerceRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except WooCommerceError as exc:
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
    store_url: str,
) -> ConnectorDocument:
    """Convert a raw WooCommerce Order object into a ConnectorDocument."""
    order_id: int = order.get("id", 0)
    order_number: str = str(order.get("number", order_id))
    status: str = order.get("status", "unknown")
    total: str = order.get("total", "0.00")
    currency: str = order.get("currency", "")
    payment_method: str = order.get("payment_method_title", order.get("payment_method", ""))
    date_created: str = order.get("date_created", "")

    billing: dict[str, Any] = order.get("billing", {})
    billing_first = billing.get("first_name", "")
    billing_last = billing.get("last_name", "")
    billing_name = f"{billing_first} {billing_last}".strip() or "Unknown"
    billing_email: str = billing.get("email", "")

    # Line items summary
    line_items: list[dict[str, Any]] = order.get("line_items", [])
    line_parts: list[str] = []
    for item in line_items:
        name = item.get("name", "")
        qty = item.get("quantity", 1)
        subtotal = item.get("subtotal", item.get("total", "0.00"))
        line_parts.append(f"  - {name} x{qty} ({subtotal} {currency})")

    content_parts: list[str] = [
        f"Order Number: #{order_number}",
        f"Status: {status}",
        f"Total: {total} {currency}",
        f"Payment Method: {payment_method}",
        f"Billing Name: {billing_name}",
        f"Billing Email: {billing_email}",
        f"Date Created: {date_created}",
    ]
    if line_parts:
        content_parts.append("Line Items:")
        content_parts.extend(line_parts)

    return ConnectorDocument(
        source_id=_stable_id("woocommerce_order", order_id),
        title=f"Order #{order_number}: {billing_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{store_url.rstrip('/')}/wp-admin/post.php?post={order_id}&action=edit",
        metadata={
            "order_id": order_id,
            "order_number": order_number,
            "status": status,
            "total": total,
            "currency": currency,
            "payment_method": payment_method,
            "billing_email": billing_email,
            "date_created": date_created,
        },
    )


def normalize_product(
    product: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    store_url: str,
) -> ConnectorDocument:
    """Convert a raw WooCommerce Product object into a ConnectorDocument."""
    product_id: int = product.get("id", 0)
    name: str = product.get("name", "")
    product_type: str = product.get("type", "simple")
    status: str = product.get("status", "publish")
    price: str = product.get("price", product.get("regular_price", "0.00"))
    stock_quantity: int | None = product.get("stock_quantity")
    sku: str = product.get("sku", "")
    permalink: str = product.get("permalink", f"{store_url.rstrip('/')}/?p={product_id}")

    # Categories
    categories: list[str] = [cat.get("name", "") for cat in product.get("categories", [])]

    # Description (strip HTML tags crudely — no lxml dependency)
    raw_description: str = product.get("description", product.get("short_description", ""))
    description: str = _strip_tags(raw_description)

    content_parts: list[str] = [
        f"Product: {name}",
        f"Type: {product_type}",
        f"Status: {status}",
        f"Price: {price}",
        f"SKU: {sku}" if sku else "SKU: N/A",
    ]
    if stock_quantity is not None:
        content_parts.append(f"Stock Quantity: {stock_quantity}")
    if categories:
        content_parts.append(f"Categories: {', '.join(categories)}")
    if description:
        content_parts.append(f"Description: {description[:500]}")

    return ConnectorDocument(
        source_id=_stable_id("woocommerce_product", product_id),
        title=f"{name} ({product_type})",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=permalink,
        metadata={
            "product_id": product_id,
            "type": product_type,
            "status": status,
            "price": price,
            "stock_quantity": stock_quantity,
            "sku": sku,
            "categories": categories,
        },
    )


def normalize_customer(
    customer: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    store_url: str,
) -> ConnectorDocument:
    """Convert a raw WooCommerce Customer object into a ConnectorDocument."""
    customer_id: int = customer.get("id", 0)
    first_name: str = customer.get("first_name", "")
    last_name: str = customer.get("last_name", "")
    email: str = customer.get("email", "")
    orders_count: int = customer.get("orders_count", 0)
    total_spent: str = customer.get("total_spent", "0.00")
    date_created: str = customer.get("date_created", "")
    username: str = customer.get("username", "")

    full_name = f"{first_name} {last_name}".strip() or username or "Unknown"

    content_parts: list[str] = [
        f"Name: {full_name}",
        f"Email: {email}",
        f"Username: {username}" if username else "",
        f"Orders Count: {orders_count}",
        f"Total Spent: {total_spent}",
        f"Date Created: {date_created}",
    ]

    billing: dict[str, Any] = customer.get("billing", {})
    if billing.get("phone"):
        content_parts.append(f"Phone: {billing['phone']}")
    if billing.get("city") or billing.get("country"):
        location_parts = [p for p in [billing.get("city", ""), billing.get("country", "")] if p]
        content_parts.append(f"Location: {', '.join(location_parts)}")

    content_parts = [p for p in content_parts if p]

    return ConnectorDocument(
        source_id=_stable_id("woocommerce_customer", customer_id),
        title=f"Customer: {full_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{store_url.rstrip('/')}/wp-admin/user-edit.php?user_id={customer_id}",
        metadata={
            "customer_id": customer_id,
            "email": email,
            "orders_count": orders_count,
            "total_spent": total_spent,
            "date_created": date_created,
        },
    )


def _strip_tags(html: str) -> str:
    """Remove HTML tags from a string without external dependencies."""
    import re
    return re.sub(r"<[^>]+>", "", html).strip()
