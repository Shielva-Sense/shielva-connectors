"""Normalise Attio API resources into ``NormalizedDocument``.

Attio responses wrap entities in ``{"data": {...}}`` envelopes. Records and
notes carry composite IDs (``{"workspace_id":..., "record_id":...}``). Each
normaliser is a pure function — no I/O, no external state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _resolve_id(node: Any) -> str:
    """Attio composite id resolver.

    Records: ``id["record_id"]`` · Notes: ``id["note_id"]`` · Tasks: ``id["task_id"]``.
    Falls back to a plain string id when the entity is already flattened.
    """
    if isinstance(node, dict):
        for key in ("record_id", "note_id", "task_id", "id"):
            v = node.get(key)
            if isinstance(v, str) and v:
                return v
        # Last-resort: deterministic stringify of the composite
        return str(node)
    if isinstance(node, str):
        return node
    return ""


def _flatten_values(values: Dict[str, Any]) -> str:
    """Turn an Attio ``values`` map into a searchable "key: value" string."""
    if not isinstance(values, dict):
        return ""
    parts: List[str] = []
    for attr, entries in values.items():
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    val = (
                        entry.get("value")
                        or entry.get("full_name")
                        or entry.get("domain")
                        or entry.get("email_address")
                        or entry.get("phone_number")
                    )
                    if val:
                        parts.append(f"{attr}: {val}")
        elif entries is not None:
            parts.append(f"{attr}: {entries}")
    return "\n".join(parts)


def _derive_title(values: Dict[str, Any], fallback: str) -> str:
    """Pick a human-readable title from common Attio attributes."""
    if not isinstance(values, dict):
        return fallback
    for attr in ("name", "full_name", "title", "domain", "email_addresses"):
        entries = values.get(attr)
        if isinstance(entries, list) and entries:
            first = entries[0]
            if isinstance(first, dict):
                for key in ("full_name", "value", "domain", "email_address"):
                    v = first.get(key)
                    if isinstance(v, str) and v:
                        return v
    return fallback


def normalize_record(
    raw: Dict[str, Any],
    object_slug: str,
    connector_id: str,
    tenant_id: str,
):
    """Turn an Attio record into a ``NormalizedDocument``."""
    from shared.base_connector import NormalizedDocument

    record = raw.get("data", raw) if isinstance(raw, dict) else {}
    if isinstance(record, list):
        record = record[0] if record else {}

    source_id = _resolve_id(record.get("id"))
    values = record.get("values") or {}
    title = _derive_title(values, fallback=source_id or f"{object_slug} record")
    content = _flatten_values(values)

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        source=f"attio.{object_slug}",
        tenant_id=tenant_id,
        connector_id=connector_id,
        created_at=_parse_dt(record.get("created_at")),
        updated_at=_parse_dt(
            record.get("updated_at") or record.get("last_modified_at")
        ),
        metadata={
            "object_slug": object_slug,
            "values": values,
            "kind": "attio.record",
        },
    )


def normalize_note(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn an Attio note into a ``NormalizedDocument``."""
    from shared.base_connector import NormalizedDocument

    note = raw.get("data", raw) if isinstance(raw, dict) else {}
    source_id = _resolve_id(note.get("id"))
    title = note.get("title") or f"Note {source_id}"
    content = note.get("content_plaintext") or note.get("content_markdown") or ""

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        source="attio.note",
        tenant_id=tenant_id,
        connector_id=connector_id,
        created_at=_parse_dt(note.get("created_at")),
        updated_at=_parse_dt(note.get("created_at")),
        metadata={
            "parent_object": note.get("parent_object"),
            "parent_record_id": note.get("parent_record_id"),
            "kind": "attio.note",
        },
    )


def normalize_task(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn an Attio task into a ``NormalizedDocument``."""
    from shared.base_connector import NormalizedDocument

    task = raw.get("data", raw) if isinstance(raw, dict) else {}
    source_id = _resolve_id(task.get("id"))
    content = task.get("content_plaintext") or ""
    # First line of the plaintext content becomes the title.
    title = content.splitlines()[0] if content else f"Task {source_id}"

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        source="attio.task",
        tenant_id=tenant_id,
        connector_id=connector_id,
        created_at=_parse_dt(task.get("created_at")),
        updated_at=_parse_dt(task.get("created_at")),
        metadata={
            "is_completed": task.get("is_completed", False),
            "deadline_at": task.get("deadline_at"),
            "linked_records": task.get("linked_records") or [],
            "kind": "attio.task",
        },
    )
