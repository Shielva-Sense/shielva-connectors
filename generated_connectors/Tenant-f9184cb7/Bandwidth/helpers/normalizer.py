"""Transform raw Bandwidth API responses → NormalizedDocument.

Multi-tenant isolation: every NormalizedDocument carries `tenant_id` and
`connector_id` so the platform's ingestion path can scope it correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    from shared.base_connector import NormalizedDocument  # type: ignore
except ImportError:  # standalone test runs
    from dataclasses import dataclass, field
    from typing import List

    @dataclass
    class NormalizedDocument:  # type: ignore[no-redef]
        id: str
        source_id: str
        title: str
        content: str
        content_type: str = "text"
        source_url: Optional[str] = None
        url: Optional[str] = None
        author: Optional[str] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        metadata: Dict[str, Any] = field(default_factory=dict)
        source: Optional[str] = None
        tenant_id: Optional[str] = None
        connector_id: Optional[str] = None
        parent_id: Optional[str] = None
        chunk_index: Optional[int] = None


def _parse_dt(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        # Bandwidth returns RFC 3339 / ISO 8601 timestamps
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
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
    return NormalizedDocument(
        id=f"{tenant_id}:{connector_id}:msg:{msg_id}",
        source_id=msg_id,
        title=f"Message {msg_id}",
        content=text,
        content_type="text/plain",
        source="bandwidth.messaging",
        created_at=_parse_dt(raw.get("time")),
        updated_at=_parse_dt(raw.get("time")),
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
    state = raw.get("state") or "unknown"
    direction = raw.get("direction") or "unknown"
    return NormalizedDocument(
        id=f"{tenant_id}:{connector_id}:call:{call_id}",
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
