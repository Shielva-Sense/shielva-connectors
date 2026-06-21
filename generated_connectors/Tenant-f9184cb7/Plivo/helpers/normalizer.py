"""Normalize Plivo API resources into NormalizedDocument.

NormalizedDocument id format is ``f"{tenant_id}_{source_id}"`` — matches the
platform-wide convention used by Wix, Gmail, Bandwidth and the rest of the
connector fleet so cross-tenant id collisions are impossible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    """Best-effort parse of a Plivo timestamp.

    Plivo returns timestamps as ISO-8601 strings in UTC (sometimes with a
    trailing ``Z``, sometimes with an explicit offset). On parse failure we
    fall back to ``datetime.now(UTC)`` so the document still ingests rather
    than failing the whole sync over a single bad field.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def normalize_message(raw: Dict[str, Any], tenant_id: str):
    """Turn a Plivo Message resource into a NormalizedDocument.

    Plivo's ``GET /Message/`` response items expose ``message_uuid``, ``from_``
    / ``to_``, ``message_state``, ``message_direction``, ``message_time``, and
    the ``text`` body. We carry the operational fields into ``metadata`` and
    use ``text`` as the document content.
    """
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("message_uuid", "") or raw.get("id", ""))
    body = str(raw.get("text", "") or "")
    src = str(raw.get("from_number", "") or raw.get("from", "") or "")
    dst = str(raw.get("to_number", "") or raw.get("to", "") or "")
    direction = str(raw.get("message_direction", "") or "")
    state = str(raw.get("message_state", "") or "")
    msg_type = str(raw.get("message_type", "") or "sms")
    created = raw.get("message_time")
    title = f"Plivo {msg_type.upper()} {direction} {src} → {dst}".strip()

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=body,
        content_type="text",
        source_url=None,
        url=None,
        author=src or None,
        created_at=_parse_dt(created),
        updated_at=_parse_dt(created),
        metadata={
            "kind": "plivo.message",
            "src": src,
            "dst": dst,
            "direction": direction,
            "state": state,
            "type": msg_type,
            "units": raw.get("units"),
            "total_amount": raw.get("total_amount"),
            "total_rate": raw.get("total_rate"),
        },
    )


def normalize_call(raw: Dict[str, Any], tenant_id: str):
    """Turn a Plivo Call resource into a NormalizedDocument.

    Plivo's ``GET /Call/`` response items expose ``call_uuid``, ``from_number``,
    ``to_number``, ``call_direction``, ``call_duration``, ``end_time``,
    ``call_state`` / ``hangup_cause_name``, and recording / billing fields.
    """
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("call_uuid", "") or raw.get("id", ""))
    src = str(raw.get("from_number", "") or raw.get("from", "") or "")
    dst = str(raw.get("to_number", "") or raw.get("to", "") or "")
    direction = str(raw.get("call_direction", "") or "")
    state = str(raw.get("call_state", "") or raw.get("hangup_cause_name", "") or "")
    duration = raw.get("call_duration") or raw.get("duration") or 0
    end = raw.get("end_time") or raw.get("initiation_time")
    title = f"Plivo CALL {direction} {src} → {dst}".strip()
    content = f"Direction: {direction}\nState: {state}\nDuration: {duration}s"

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        source_url=None,
        url=None,
        author=src or None,
        created_at=_parse_dt(raw.get("initiation_time") or end),
        updated_at=_parse_dt(end),
        metadata={
            "kind": "plivo.call",
            "src": src,
            "dst": dst,
            "direction": direction,
            "state": state,
            "duration_s": duration,
            "hangup_cause": raw.get("hangup_cause_name"),
            "hangup_source": raw.get("hangup_source"),
            "bill_duration": raw.get("bill_duration"),
            "total_amount": raw.get("total_amount"),
            "total_rate": raw.get("total_rate"),
            "parent_call_uuid": raw.get("parent_call_uuid"),
        },
    )
