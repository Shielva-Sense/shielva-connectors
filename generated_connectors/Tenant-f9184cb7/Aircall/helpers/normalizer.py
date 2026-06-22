"""Transforms raw Aircall API responses into NormalizedDocument objects."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import structlog
from shared.base_connector import NormalizedDocument

logger = structlog.get_logger(__name__)


def _to_dt(epoch: Optional[int]) -> Optional[datetime]:
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    except Exception:
        return None


def normalize_call(
    call: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert an Aircall /calls/{id} object to a NormalizedDocument."""
    call_id = str(call.get("id", ""))
    direction = call.get("direction", "")
    status = call.get("status", "")
    started_at = _to_dt(call.get("started_at"))
    ended_at = _to_dt(call.get("ended_at"))
    duration = call.get("duration", 0) or 0
    raw_digits = call.get("raw_digits", "") or ""

    user = call.get("user") or {}
    contact = call.get("contact") or {}
    number = call.get("number") or {}

    title_bits = [
        direction.title() or "Call",
        f"with {contact.get('first_name', raw_digits) or raw_digits}".strip(),
    ]
    title = " ".join(b for b in title_bits if b) or f"Call {call_id}"

    body_lines = [
        f"Direction: {direction}",
        f"Status: {status}",
        f"Duration: {duration}s",
        f"From/To: {raw_digits}",
        f"Agent: {user.get('name', '')}",
        f"Contact: {contact.get('first_name', '')} {contact.get('last_name', '')}".strip(),
    ]
    if call.get("voicemail"):
        body_lines.append(f"Voicemail: {call['voicemail']}")
    if call.get("recording"):
        body_lines.append(f"Recording: {call['recording']}")
    content = "\n".join(body_lines)

    return NormalizedDocument(
        id=f"{connector_id}_{call_id}",
        source_id=call_id,
        title=title,
        content=content,
        content_type="text",
        source_url=call.get("direct_link", ""),
        author=user.get("name") or user.get("email") or "",
        created_at=started_at,
        updated_at=ended_at or started_at,
        source="aircall_connector",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "direction": direction,
            "status": status,
            "duration": duration,
            "raw_digits": raw_digits,
            "user_id": user.get("id"),
            "contact_id": contact.get("id"),
            "number_id": number.get("id"),
            "voicemail": call.get("voicemail"),
            "recording": call.get("recording"),
            "missed_call_reason": call.get("missed_call_reason"),
        },
    )
