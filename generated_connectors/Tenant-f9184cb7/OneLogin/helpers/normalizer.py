"""Normalize OneLogin API objects → Shielva ``NormalizedDocument``.

All transforms live here (SOC). The connector calls these per-record from
``sync()`` and never inlines field-mapping logic in its own body.

NormalizedDocument id contract: ``f"{tenant_id}_{source_id}"`` (multi-tenant
isolation guarantee — required by the platform).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from shared.base_connector import NormalizedDocument


def _parse_iso(value: Any) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def normalize_user(
    raw: Dict[str, Any], connector_id: str, tenant_id: str
) -> NormalizedDocument:
    """Map a OneLogin user object → ``NormalizedDocument``.

    The ``id`` is tenant-scoped: ``f"{tenant_id}_{source_id}"`` per system rules.
    """
    user_id = str(raw.get("id", ""))
    email = raw.get("email", "") or ""
    first = raw.get("firstname", "") or ""
    last = raw.get("lastname", "") or ""
    title = f"{first} {last}".strip() or email or user_id
    content_parts = [
        f"Email: {email}",
        f"Username: {raw.get('username', '')}",
        f"Status: {raw.get('status', '')}",
        f"State: {raw.get('state', '')}",
        f"Department: {raw.get('department', '')}",
        f"Title: {raw.get('title', '')}",
    ]
    return NormalizedDocument(
        id=f"{tenant_id}_{user_id}",
        source_id=user_id,
        title=title,
        content="\n".join(p for p in content_parts if p.strip().rstrip(":")),
        content_type="text",
        author=email or None,
        created_at=_parse_iso(raw.get("created_at")),
        updated_at=_parse_iso(raw.get("updated_at")),
        metadata={
            "kind": "onelogin.user",
            "role_ids": raw.get("role_id", []),
            "group_id": raw.get("group_id"),
            "manager_user_id": raw.get("manager_user_id"),
            "department": raw.get("department"),
            "status": raw.get("status"),
            "state": raw.get("state"),
        },
        source="onelogin",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_event(
    raw: Dict[str, Any], connector_id: str, tenant_id: str
) -> NormalizedDocument:
    """Map a OneLogin event → ``NormalizedDocument``."""
    event_id = str(raw.get("id", ""))
    notes = raw.get("notes", "") or ""
    actor = raw.get("actor_user_name", "") or raw.get("user_name", "")
    title = f"{raw.get('event_type_id', '')}: {actor}".strip(": ")
    return NormalizedDocument(
        id=f"{tenant_id}_{event_id}",
        source_id=event_id,
        title=title or f"Event {event_id}",
        content=notes or str(raw),
        content_type="text",
        author=actor or None,
        created_at=_parse_iso(raw.get("created_at")),
        metadata={
            "kind": "onelogin.event",
            "event_type_id": raw.get("event_type_id"),
            "ipaddr": raw.get("ipaddr"),
            "user_id": raw.get("user_id"),
        },
        source="onelogin",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
