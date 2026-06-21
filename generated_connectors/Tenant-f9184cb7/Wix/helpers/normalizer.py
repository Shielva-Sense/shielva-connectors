"""Normalize Wix API resources into NormalizedDocument."""
from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_product(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Wix Stores product into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    product = raw.get("product", raw) if isinstance(raw, dict) else {}
    source_id = product.get("id", "")
    name = product.get("name", "")
    description = product.get("description", "") or product.get("plainDescription", "")
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=description,
        content_type="text",
        source_url=product.get("productPageUrl", {}).get("url") if isinstance(product.get("productPageUrl"), dict) else None,
        url=None,
        author=None,
        created_at=_parse_dt(product.get("createdDate")),
        updated_at=_parse_dt(product.get("lastUpdated") or product.get("updatedDate")),
        metadata={
            "sku": product.get("sku", ""),
            "price": (product.get("price", {}) or {}).get("price"),
            "currency": (product.get("price", {}) or {}).get("currency", ""),
            "stock": (product.get("stock", {}) or {}).get("quantity"),
            "kind": "wix.product",
        },
    )


def normalize_order(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Wix Ecom order into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    order = raw.get("order", raw) if isinstance(raw, dict) else {}
    source_id = order.get("id", "")
    number = order.get("number", "")
    buyer = (order.get("buyerInfo", {}) or {}).get("email", "")
    totals = order.get("priceSummary", {}) or {}
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=f"Order {number}" if number else f"Order {source_id}",
        content=str(order.get("status", "")),
        content_type="text",
        author=buyer,
        created_at=_parse_dt(order.get("createdDate")),
        updated_at=_parse_dt(order.get("updatedDate")),
        metadata={
            "number": number,
            "status": order.get("status", ""),
            "total": (totals.get("total", {}) or {}).get("amount"),
            "currency": order.get("currency", ""),
            "kind": "wix.order",
        },
    )
