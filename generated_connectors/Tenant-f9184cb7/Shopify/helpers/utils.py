from __future__ import annotations

import asyncio
import hashlib
import re
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ShopifyAuthError, ShopifyError, ShopifyRateLimitError
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


def _strip_html(html: str) -> str:
    """Remove HTML tags from a string."""
    if not html:
        return ""
    return re.sub(r"<[^>]+>", "", html).strip()


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
    last_exc: ShopifyError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ShopifyAuthError:
            raise
        except ShopifyRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ShopifyError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Normalizers ──────────────────────────────────────────────────────────────


def normalize_order(
    order: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    shop_url: str,
) -> ConnectorDocument:
    """Convert a raw Shopify Order object into a ConnectorDocument."""
    order_id = order.get("id", 0)
    order_number = order.get("order_number", order_id)
    financial_status = order.get("financial_status", "")
    fulfillment_status = order.get("fulfillment_status") or "unfulfilled"
    total_price = order.get("total_price", "0.00")
    currency = order.get("currency", "")
    created_at = order.get("created_at", "")

    customer = order.get("customer") or {}
    customer_email = customer.get("email", "") or order.get("email", "")
    first = customer.get("first_name", "")
    last = customer.get("last_name", "")
    customer_name = f"{first} {last}".strip() or customer_email or "Guest"

    line_items: list[dict[str, Any]] = order.get("line_items", [])
    line_item_lines = [
        f"  - {item.get('name', '')} x{item.get('quantity', 1)} @ {item.get('price', '0.00')}"
        for item in line_items
    ]

    content_parts = [
        f"Order #{order_number}",
        f"Customer: {customer_name}",
        f"Email: {customer_email}",
        f"Financial status: {financial_status}",
        f"Fulfillment status: {fulfillment_status}",
        f"Total: {total_price} {currency}",
        f"Created at: {created_at}",
        "Line items:",
        *line_item_lines,
    ]

    host = shop_url.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"

    return ConnectorDocument(
        source_id=_stable_id(order_id),
        title=f"Order #{order_number}: {customer_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{host}/admin/orders/{order_id}",
        metadata={
            "order_id": order_id,
            "order_number": order_number,
            "financial_status": financial_status,
            "fulfillment_status": fulfillment_status,
            "total_price": total_price,
            "currency": currency,
            "customer_email": customer_email,
            "created_at": created_at,
        },
    )


def normalize_product(
    product: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    shop_url: str,
) -> ConnectorDocument:
    """Convert a raw Shopify Product object into a ConnectorDocument."""
    product_id = product.get("id", 0)
    title = product.get("title", "Untitled Product")
    vendor = product.get("vendor", "")
    product_type = product.get("product_type", "")
    status = product.get("status", "")
    tags = product.get("tags", "")
    body_html = product.get("body_html", "") or ""
    body_text = _strip_html(body_html)

    variants: list[dict[str, Any]] = product.get("variants", [])
    variants_count = len(variants)
    variant_lines = [
        f"  - {v.get('title', 'Default')} @ {v.get('price', '0.00')} (SKU: {v.get('sku', 'N/A')})"
        for v in variants[:10]
    ]

    content_parts = [
        f"Product: {title}",
        f"Vendor: {vendor}",
        f"Type: {product_type}",
        f"Status: {status}",
        f"Tags: {tags}",
    ]
    if body_text:
        content_parts.append(f"Description: {body_text}")
    if variant_lines:
        content_parts.append(f"Variants ({variants_count}):")
        content_parts.extend(variant_lines)

    host = shop_url.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"

    return ConnectorDocument(
        source_id=_stable_id(product_id),
        title=f"{title} — {vendor}" if vendor else title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{host}/admin/products/{product_id}",
        metadata={
            "product_id": product_id,
            "product_type": product_type,
            "vendor": vendor,
            "status": status,
            "variants_count": variants_count,
            "tags": tags,
        },
    )


def normalize_customer(
    customer: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    shop_url: str,
) -> ConnectorDocument:
    """Convert a raw Shopify Customer object into a ConnectorDocument."""
    customer_id = customer.get("id", 0)
    first = customer.get("first_name", "")
    last = customer.get("last_name", "")
    email = customer.get("email", "")
    phone = customer.get("phone", "") or ""
    orders_count = customer.get("orders_count", 0)
    total_spent = customer.get("total_spent", "0.00")
    tags = customer.get("tags", "")
    created_at = customer.get("created_at", "")
    accepts_marketing = customer.get("accepts_marketing", False)
    verified_email = customer.get("verified_email", False)

    full_name = f"{first} {last}".strip() or email

    content_parts = [
        f"Customer: {full_name}",
        f"Email: {email}",
    ]
    if phone:
        content_parts.append(f"Phone: {phone}")
    content_parts += [
        f"Orders count: {orders_count}",
        f"Total spent: {total_spent}",
        f"Accepts marketing: {accepts_marketing}",
        f"Verified email: {verified_email}",
        f"Tags: {tags}",
        f"Created at: {created_at}",
    ]

    host = shop_url.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"

    return ConnectorDocument(
        source_id=_stable_id(customer_id),
        title=f"Customer: {full_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{host}/admin/customers/{customer_id}",
        metadata={
            "customer_id": customer_id,
            "email": email,
            "orders_count": orders_count,
            "total_spent": total_spent,
            "tags": tags,
            "created_at": created_at,
        },
    )
