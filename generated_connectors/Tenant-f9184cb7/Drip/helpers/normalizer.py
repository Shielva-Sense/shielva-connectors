"""Normalize Drip API resources into ``NormalizedDocument``.

Drip data isn't naturally document-shaped (it's a marketing CRM), but to fit
the Shielva KB ingest pipeline we project subscribers, campaigns and orders
into ``NormalizedDocument`` with stable tenant-scoped ids.

NormalizedDocument id contract:
    id = f"{tenant_id}_{source_id}"

This matches every other Shielva connector and lets the KB layer dedupe
across re-syncs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    """Parse Drip's ISO-8601 strings; fall back to ``now()`` on bad input."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_subscriber(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Drip subscriber payload into a NormalizedDocument.

    Drip wraps single records inside ``{"subscribers":[{...}]}``; callers may
    pass either the wrapped envelope or a single subscriber dict.
    """
    from shared.base_connector import NormalizedDocument

    if isinstance(raw, dict) and "subscribers" in raw:
        items = raw.get("subscribers") or []
        sub = items[0] if items else {}
    else:
        sub = raw if isinstance(raw, dict) else {}

    source_id = str(sub.get("id", "") or sub.get("email", ""))
    email = sub.get("email", "")
    first = sub.get("first_name") or ""
    last = sub.get("last_name") or ""
    name = (f"{first} {last}").strip() or email or source_id
    content = " | ".join(p for p in [email, first, last, sub.get("status", "")] if p)

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or email,
        content=content,
        content_type="text",
        author=email,
        created_at=_parse_dt(sub.get("created_at")),
        updated_at=_parse_dt(sub.get("updated_at") or sub.get("created_at")),
        metadata={
            "email": email,
            "status": sub.get("status", ""),
            "tags": sub.get("tags", []) or [],
            "custom_fields": sub.get("custom_fields", {}) or {},
            "time_zone": sub.get("time_zone", ""),
            "ip_address": sub.get("ip_address", ""),
            "kind": "drip.subscriber",
        },
    )


def normalize_campaign(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Drip campaign payload into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    if isinstance(raw, dict) and "campaigns" in raw:
        items = raw.get("campaigns") or []
        campaign = items[0] if items else {}
    else:
        campaign = raw if isinstance(raw, dict) else {}

    source_id = str(campaign.get("id", ""))
    name = campaign.get("name", "") or f"Campaign {source_id}"
    summary = campaign.get("subject", "") or campaign.get("from_name", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=summary,
        content_type="text",
        author=campaign.get("from_email", ""),
        created_at=_parse_dt(campaign.get("created_at")),
        updated_at=_parse_dt(campaign.get("updated_at") or campaign.get("created_at")),
        metadata={
            "status": campaign.get("status", ""),
            "from_name": campaign.get("from_name", ""),
            "from_email": campaign.get("from_email", ""),
            "subject": campaign.get("subject", ""),
            "kind": "drip.campaign",
        },
    )


def normalize_order(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Drip order payload into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    if isinstance(raw, dict) and "orders" in raw:
        items = raw.get("orders") or []
        order = items[0] if items else {}
    else:
        order = raw if isinstance(raw, dict) else {}

    source_id = str(order.get("id", "") or order.get("provider_order_id", ""))
    number = order.get("provider_order_id", "") or source_id
    email = order.get("email", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=f"Order {number}",
        content=str(order.get("financial_state", "") or order.get("fulfillment_state", "")),
        content_type="text",
        author=email,
        created_at=_parse_dt(order.get("occurred_at") or order.get("created_at")),
        updated_at=_parse_dt(order.get("updated_at") or order.get("occurred_at")),
        metadata={
            "email": email,
            "provider": order.get("provider", ""),
            "amount": order.get("amount"),
            "currency": order.get("currency", ""),
            "financial_state": order.get("financial_state", ""),
            "fulfillment_state": order.get("fulfillment_state", ""),
            "items_count": len(order.get("items", []) or []),
            "kind": "drip.order",
        },
    )
