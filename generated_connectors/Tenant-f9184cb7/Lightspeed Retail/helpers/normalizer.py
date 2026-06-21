"""Transforms raw Lightspeed Retail API responses into NormalizedDocument objects."""
from typing import Any, Dict, Optional

import structlog
from shared.base_connector import NormalizedDocument

from helpers.utils import parse_lightspeed_datetime

logger = structlog.get_logger(__name__)


def _safe_float(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def normalize_item(
    item: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert a Lightspeed Retail Item into a NormalizedDocument."""
    item_id = str(item.get("itemID", "")) or str(item.get("systemSku", ""))
    description = item.get("description", "") or "(no description)"
    default_price = _safe_float(item.get("defaultCost") and item.get("Prices", {}).get("ItemPrice", [{}])[0].get("amount"))
    if not default_price:
        # Lightspeed Prices is a nested envelope: {"ItemPrice": [{"amount": "9.99", "useType": "Default"}, ...]}
        prices = item.get("Prices", {}) or {}
        ip = prices.get("ItemPrice") if isinstance(prices, dict) else None
        if isinstance(ip, list):
            for p in ip:
                if isinstance(p, dict) and p.get("useType", "").lower() == "default":
                    default_price = _safe_float(p.get("amount"))
                    break
        elif isinstance(ip, dict):
            default_price = _safe_float(ip.get("amount"))

    default_cost = _safe_float(item.get("defaultCost"))
    created_at = parse_lightspeed_datetime(item.get("createTime"))
    updated_at = parse_lightspeed_datetime(item.get("timeStamp"))

    return NormalizedDocument(
        id=f"{connector_id}_item_{item_id}",
        source_id=item_id,
        title=description,
        content=description,
        content_type="text",
        source_url=None,
        author=None,
        created_at=created_at,
        updated_at=updated_at,
        source="lightspeed_retail",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "item_id": item_id,
            "category_id": item.get("categoryID"),
            "manufacturer_id": item.get("manufacturerID"),
            "default_cost": default_cost,
            "default_price": default_price,
            "item_type": item.get("itemType"),
            "custom_sku": item.get("customSku"),
            "manufacturer_sku": item.get("manufacturerSku"),
            "tax": item.get("tax"),
        },
    )


def normalize_sale(
    sale: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert a Lightspeed Retail Sale into a NormalizedDocument."""
    sale_id = str(sale.get("saleID", ""))
    completed = str(sale.get("completed", "")).lower() == "true"
    total = _safe_float(sale.get("total"))
    customer_id = sale.get("customerID")
    title = f"Sale #{sale_id} ({'completed' if completed else 'open'})"
    created_at = parse_lightspeed_datetime(sale.get("createTime") or sale.get("timeStamp"))
    updated_at = parse_lightspeed_datetime(sale.get("timeStamp"))

    return NormalizedDocument(
        id=f"{connector_id}_sale_{sale_id}",
        source_id=sale_id,
        title=title,
        content=f"Sale {sale_id} — total {total} — customer {customer_id or '?'}",
        content_type="text",
        source_url=None,
        author=str(sale.get("employeeID", "")) or None,
        created_at=created_at,
        updated_at=updated_at,
        source="lightspeed_retail",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "sale_id": sale_id,
            "customer_id": customer_id,
            "shop_id": sale.get("shopID"),
            "register_id": sale.get("registerID"),
            "employee_id": sale.get("employeeID"),
            "completed": completed,
            "total": total,
            "discount_percent": sale.get("discountPercent"),
        },
    )
