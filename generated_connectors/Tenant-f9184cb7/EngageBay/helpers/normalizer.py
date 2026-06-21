"""Normalize EngageBay API payloads.

Two surfaces:

1. `flatten_contact` / `flatten_deal` / `flatten_task` — flat dicts (used by
   public `get_*()` methods that expose the resource to a caller).
2. `normalize_contact_doc` / `normalize_deal_doc` / `normalize_task_doc` —
   `NormalizedDocument` (used by `sync()` to feed the Shielva KB).

Nothing here issues HTTP. Pure transforms only.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── flat-dict flatten helpers ──────────────────────────────────────────────

def _properties_to_map(properties: List[Dict[str, Any]]) -> Dict[str, Any]:
    """EngageBay returns `properties: [{name, value, field_type}…]`. Flatten by name."""
    flat: Dict[str, Any] = {}
    for prop in properties or []:
        if not isinstance(prop, dict):
            continue
        name = prop.get("name")
        if name:
            flat[name] = prop.get("value")
    return flat


def flatten_contact(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten an EngageBay contact response into id + key/value props + tags."""
    properties = raw.get("properties", []) or []
    flat = _properties_to_map(properties)
    return {
        "id": str(raw.get("id")) if raw.get("id") is not None else None,
        "email": flat.get("email"),
        "first_name": flat.get("first_name"),
        "last_name": flat.get("last_name"),
        "company": flat.get("company"),
        "phone": flat.get("phone"),
        "tags": [t.get("tag") if isinstance(t, dict) else t for t in raw.get("tags", []) or []],
        "properties": flat,
        "raw": raw,
    }


def flatten_deal(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(raw.get("id")) if raw.get("id") is not None else None,
        "name": raw.get("name"),
        "expected_value": raw.get("expected_value"),
        "milestone": raw.get("milestone"),
        "pipeline_id": raw.get("pipeline_id"),
        "contact_ids": raw.get("contact_ids", []) or [],
        "raw": raw,
    }


def flatten_task(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(raw.get("id")) if raw.get("id") is not None else None,
        "name": raw.get("name"),
        "due_date": raw.get("due_date"),
        "status": raw.get("status"),
        "owner_id": raw.get("owner_id"),
        "raw": raw,
    }


# Back-compat aliases (older code paths called these `normalize_*`).
normalize_contact = flatten_contact
normalize_deal = flatten_deal
normalize_task = flatten_task


# ── NormalizedDocument producers (for sync → KB ingest) ────────────────────

def _epoch_ms_to_dt(value: Any) -> Optional[datetime]:
    """EngageBay timestamps are unix-epoch milliseconds."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def normalize_contact_doc(raw: Dict[str, Any], connector_id: str, tenant_id: str):
    """Turn an EngageBay contact into a `NormalizedDocument`."""
    from shared.base_connector import NormalizedDocument

    flat = flatten_contact(raw)
    source_id = flat["id"] or ""
    name_parts = [flat.get("first_name") or "", flat.get("last_name") or ""]
    title = " ".join(p for p in name_parts if p).strip() or flat.get("email") or f"Contact {source_id}"
    content = " ".join(
        v for v in [title, flat.get("email") or "", flat.get("phone") or "", flat.get("company") or ""] if v
    ).strip()
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        author=flat.get("email"),
        created_at=_epoch_ms_to_dt(raw.get("created_time") or raw.get("created_at")),
        updated_at=_epoch_ms_to_dt(raw.get("updated_time") or raw.get("updated_at")),
        metadata={
            "email": flat.get("email"),
            "phone": flat.get("phone"),
            "company": flat.get("company"),
            "tags": flat.get("tags", []),
            "properties": flat.get("properties", {}),
            "kind": "engagebay.contact",
        },
        source="engagebay",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_deal_doc(raw: Dict[str, Any], connector_id: str, tenant_id: str):
    """Turn an EngageBay deal into a `NormalizedDocument`."""
    from shared.base_connector import NormalizedDocument

    flat = flatten_deal(raw)
    source_id = flat["id"] or ""
    title = flat.get("name") or f"Deal {source_id}"
    expected_value = flat.get("expected_value")
    milestone = flat.get("milestone") or ""
    content = f"{title} — {milestone} ({expected_value})".strip(" —")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        created_at=_epoch_ms_to_dt(raw.get("created_time") or raw.get("created_at")),
        updated_at=_epoch_ms_to_dt(raw.get("updated_time") or raw.get("updated_at")),
        metadata={
            "expected_value": flat.get("expected_value"),
            "milestone": flat.get("milestone"),
            "pipeline_id": flat.get("pipeline_id"),
            "contact_ids": flat.get("contact_ids", []),
            "kind": "engagebay.deal",
        },
        source="engagebay",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_task_doc(raw: Dict[str, Any], connector_id: str, tenant_id: str):
    """Turn an EngageBay task into a `NormalizedDocument`."""
    from shared.base_connector import NormalizedDocument

    flat = flatten_task(raw)
    source_id = flat["id"] or ""
    title = flat.get("name") or f"Task {source_id}"
    status = flat.get("status") or ""
    content = f"{title} — {status}".strip(" —")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        created_at=_epoch_ms_to_dt(raw.get("created_time") or raw.get("created_at")),
        updated_at=_epoch_ms_to_dt(raw.get("updated_time") or raw.get("updated_at")),
        metadata={
            "due_date": flat.get("due_date"),
            "owner_id": flat.get("owner_id"),
            "status": flat.get("status"),
            "kind": "engagebay.task",
        },
        source="engagebay",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


# ── build/validate helpers ─────────────────────────────────────────────────

def build_contact_properties(properties: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate + normalize an inbound properties list for create/update calls.

    Each item must have `name` and `value`. `field_type` defaults to "TEXT".
    """
    out: List[Dict[str, Any]] = []
    for prop in properties or []:
        if not isinstance(prop, dict):
            continue
        name = prop.get("name")
        if not name:
            continue
        out.append({
            "name": name,
            "value": prop.get("value"),
            "field_type": prop.get("field_type", "TEXT"),
        })
    return out


def coerce_id(value: Any) -> Optional[str]:
    """Coerce an int/str id to a string, preserving None."""
    return None if value is None else str(value)
