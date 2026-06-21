"""Map raw Bandwidth API payloads → NormalizedDocument.

Multi-tenant: every NormalizedDocument id has the form
`{tenant_id}_{source_id}` so two tenants with the same Bandwidth IDs
produce distinct documents.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.base_connector import NormalizedDocument


def _parse_dt(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw)
    # Use +00:00 instead of Z (Python <3.11 fromisoformat doesn't accept Z).
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def normalize_message(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Map a Bandwidth /messages payload to a NormalizedDocument."""
    msg_id = str(raw.get("id") or "")
    text = raw.get("text") or ""
    created = _parse_dt(raw.get("time"))
    return NormalizedDocument(
        id=f"{tenant_id}_{msg_id}",
        source_id=msg_id,
        title=f"Message {msg_id}",
        content=text,
        content_type="text/plain",
        source="bandwidth.messaging",
        created_at=created,
        updated_at=created,
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "direction": raw.get("direction"),
            "from": raw.get("from"),
            "to": raw.get("to"),
            "application_id": raw.get("applicationId"),
            "segment_count": raw.get("segmentCount"),
            "media": raw.get("media") or [],
        },
    )


def normalize_call(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Map a Bandwidth /calls payload to a NormalizedDocument."""
    call_id = str(raw.get("callId") or raw.get("id") or "")
    direction = raw.get("direction") or "unknown"
    state = raw.get("state") or "unknown"
    return NormalizedDocument(
        id=f"{tenant_id}_{call_id}",
        source_id=call_id,
        title=f"Call {call_id} ({direction}, {state})",
        content=f"Call {call_id} from {raw.get('from')} to {raw.get('to')} ended {state}.",
        content_type="text/plain",
        source="bandwidth.voice",
        created_at=_parse_dt(raw.get("startTime")),
        updated_at=_parse_dt(raw.get("endTime") or raw.get("startTime")),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "direction": direction,
            "state": state,
            "from": raw.get("from"),
            "to": raw.get("to"),
            "application_id": raw.get("applicationId"),
            "answer_time": raw.get("answerTime"),
            "end_time": raw.get("endTime"),
        },
    )
