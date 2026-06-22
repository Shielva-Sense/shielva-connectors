from __future__ import annotations

import asyncio
import hashlib
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import BigCommerceAuthError, BigCommerceError, BigCommerceRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(prefix: str, raw_id: int | str) -> str:
    """Return the first 16 hex chars of SHA-256('{prefix}:{raw_id}')."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


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
    Rate-limit errors honour the Retry-After/X-Rate-Limit-Time-Reset-Ms value.
    """
    last_exc: BigCommerceError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except BigCommerceAuthError:
            raise
        except BigCommerceRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except BigCommerceError as exc:
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


def normalize_product(
    product: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    store_hash: str,
) -> ConnectorDocument:
    """Convert a raw BigCommerce v3 Product object into a ConnectorDocument."""
    product_id = product.get("id", 0)
    name = product.get("name", "Untitled Product")
    brand_name = product.get("brand_name", "") or ""
    sku = product.get("sku", "") or ""
    price = product.get("price", 0)
    sale_price = product.get("sale_price", 0)
    availability = product.get("availability", "")
    condition = product.get("condition", "")
    type_ = product.get("type", "")
    weight = product.get("weight", 0)
    categories = product.get("categories", [])
    description_html = product.get("description", "") or ""
    description_text = _strip_html(description_html)
    inventory_level = product.get("inventory_level", 0)
    inventory_tracking = product.get("inventory_tracking", "none")

    content_parts = [
        f"Product: {name}",
    ]
    if brand_name:
        content_parts.append(f"Brand: {brand_name}")
    if sku:
        content_parts.append(f"SKU: {sku}")
    content_parts += [
        f"Price: {price}",
        f"Availability: {availability}",
        f"Type: {type_}",
        f"Condition: {condition}",
        f"Weight: {weight}",
        f"Inventory level: {inventory_level}",
        f"Inventory tracking: {inventory_tracking}",
    ]
    if sale_price:
        content_parts.append(f"Sale price: {sale_price}")
    if categories:
        content_parts.append(f"Categories: {categories}")
    if description_text:
        content_parts.append(f"Description: {description_text}")

    return ConnectorDocument(
        source_id=_stable_id("product", product_id),
        title=f"{name} — {brand_name}" if brand_name else name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://store-{store_hash}.mybigcommerce.com/manage/products/{product_id}/edit",
        metadata={
            "product_id": product_id,
            "sku": sku,
            "brand_name": brand_name,
            "price": price,
            "sale_price": sale_price,
            "availability": availability,
            "condition": condition,
            "type": type_,
            "inventory_level": inventory_level,
            "categories": categories,
        },
    )


def normalize_order(
    order: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    store_hash: str,
) -> ConnectorDocument:
    """Convert a raw BigCommerce v2 Order object into a ConnectorDocument."""
    order_id = order.get("id", 0)
    status = order.get("status", "")
    total_inc_tax = order.get("total_inc_tax", "0.00")
    total_ex_tax = order.get("total_ex_tax", "0.00")
    currency_code = order.get("currency_code", "")
    date_created = order.get("date_created", "")
    date_modified = order.get("date_modified", "")
    payment_method = order.get("payment_method", "")
    items_total = order.get("items_total", 0)
    items_shipped = order.get("items_shipped", 0)
    refunded_amount = order.get("refunded_amount", "0.00")

    billing = order.get("billing_address", {}) or {}
    first = billing.get("first_name", "")
    last = billing.get("last_name", "")
    email = billing.get("email", "") or order.get("customer_email", "")
    customer_name = f"{first} {last}".strip() or email or "Guest"

    content_parts = [
        f"Order #{order_id}",
        f"Customer: {customer_name}",
        f"Email: {email}",
        f"Status: {status}",
        f"Total (inc. tax): {total_inc_tax} {currency_code}",
        f"Total (ex. tax): {total_ex_tax} {currency_code}",
        f"Payment method: {payment_method}",
        f"Items total: {items_total}",
        f"Items shipped: {items_shipped}",
        f"Refunded amount: {refunded_amount}",
        f"Created at: {date_created}",
        f"Modified at: {date_modified}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("order", order_id),
        title=f"Order #{order_id}: {customer_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://store-{store_hash}.mybigcommerce.com/manage/orders/{order_id}",
        metadata={
            "order_id": order_id,
            "status": status,
            "total_inc_tax": total_inc_tax,
            "currency_code": currency_code,
            "date_created": date_created,
            "payment_method": payment_method,
            "items_total": items_total,
            "items_shipped": items_shipped,
            "customer_email": email,
        },
    )


def normalize_customer(
    customer: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    store_hash: str,
) -> ConnectorDocument:
    """Convert a raw BigCommerce v3 Customer object into a ConnectorDocument."""
    customer_id = customer.get("id", 0)
    first = customer.get("first_name", "")
    last = customer.get("last_name", "")
    email = customer.get("email", "")
    company = customer.get("company", "") or ""
    phone = customer.get("phone", "") or ""
    date_created = customer.get("date_created", "")
    date_modified = customer.get("date_modified", "")
    accepts_product_review_abandoned_cart_emails = customer.get(
        "accepts_product_review_abandoned_cart_emails", False
    )
    store_credit_amounts = customer.get("store_credit_amounts", [])
    store_credit = store_credit_amounts[0].get("amount", 0) if store_credit_amounts else 0
    customer_group_id = customer.get("customer_group_id", 0)

    full_name = f"{first} {last}".strip() or email

    content_parts = [
        f"Customer: {full_name}",
        f"Email: {email}",
    ]
    if company:
        content_parts.append(f"Company: {company}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    content_parts += [
        f"Customer group ID: {customer_group_id}",
        f"Store credit: {store_credit}",
        f"Accepts marketing emails: {accepts_product_review_abandoned_cart_emails}",
        f"Created at: {date_created}",
        f"Modified at: {date_modified}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("customer", customer_id),
        title=f"Customer: {full_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://store-{store_hash}.mybigcommerce.com/manage/customers/{customer_id}/edit",
        metadata={
            "customer_id": customer_id,
            "email": email,
            "company": company,
            "phone": phone,
            "customer_group_id": customer_group_id,
            "store_credit": store_credit,
            "date_created": date_created,
        },
    )
