"""Normalize Loggly API resources into NormalizedDocument."""
import json
from datetime import datetime, timezone
from typing import Any, Dict


def _parse_ms_epoch(value: Any) -> datetime:
    """Parse a Loggly timestamp (ms since epoch) into UTC datetime."""
    if isinstance(value, datetime):
        return value
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        if isinstance(value, str) and value.isdigit():
            return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        pass
    return datetime.now(timezone.utc)


def normalize_event(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Loggly event into a NormalizedDocument.

    Loggly events come back as `{"id": "...", "timestamp": ms_since_epoch,
    "logmsg": "...", "event": {...}, "tags": [...]}`.
    """
    from shared.base_connector import NormalizedDocument

    event = raw if isinstance(raw, dict) else {}
    source_id = str(event.get("id") or event.get("_id") or "")
    created_at = _parse_ms_epoch(event.get("timestamp"))

    payload = event.get("event") or {}
    if isinstance(payload, dict):
        content = json.dumps(payload, ensure_ascii=False, default=str)
    else:
        content = str(payload)

    title = str(event.get("logmsg") or f"Loggly event {source_id or 'unknown'}")[:512]

    tags = event.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]

    doc_id = f"{tenant_id}_{source_id}" if source_id else f"{tenant_id}_{int(created_at.timestamp())}"

    return NormalizedDocument(
        id=doc_id,
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        source="loggly.events",
        author=None,
        created_at=created_at,
        updated_at=created_at,
        metadata={
            "tags": tags,
            "kind": "loggly.event",
            "raw": event,
        },
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
