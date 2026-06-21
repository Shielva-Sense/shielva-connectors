"""Map raw Vonage API payloads → NormalizedDocument.

Multi-tenant: every NormalizedDocument id has the form
`{tenant_id}_{source_id}` so two tenants with the same Vonage IDs
produce distinct documents.
"""

from __future__ import annotations

from typing import Any, Dict

from shared.base_connector import NormalizedDocument

from helpers.utils import parse_dt


def normalize_sms(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Map a Vonage /search/message payload to a NormalizedDocument."""
    msg_id = str(raw.get("message-id") or raw.get("messageId") or raw.get("id") or "")
    body = raw.get("body") or raw.get("text") or ""
    received = parse_dt(raw.get("date-received") or raw.get("date_received") or raw.get("time"))
    return NormalizedDocument(
        id=f"{tenant_id}_{msg_id}",
        source_id=msg_id,
        title=f"SMS {msg_id}",
        content=body,
        content_type="text/plain",
        source="vonage.sms",
        created_at=received,
        updated_at=received,
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "from": raw.get("from"),
            "to": raw.get("to"),
            "direction": raw.get("type") or raw.get("direction"),
            "network": raw.get("network"),
            "price": raw.get("price"),
            "status": raw.get("status"),
        },
    )


def normalize_call(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Map a Vonage /v1/calls payload to a NormalizedDocument."""
    call_id = str(raw.get("uuid") or raw.get("call_uuid") or raw.get("id") or "")
    direction = raw.get("direction") or "unknown"
    status = raw.get("status") or "unknown"
    from_ = raw.get("from") or {}
    to = raw.get("to") or {}
    if isinstance(from_, dict):
        from_disp = from_.get("number") or ""
    else:
        from_disp = str(from_)
    if isinstance(to, dict):
        to_disp = to.get("number") or ""
    else:
        to_disp = str(to)
    return NormalizedDocument(
        id=f"{tenant_id}_{call_id}",
        source_id=call_id,
        title=f"Call {call_id} ({direction}, {status})",
        content=f"Call {call_id} from {from_disp} to {to_disp} ended {status}.",
        content_type="text/plain",
        source="vonage.voice",
        created_at=parse_dt(raw.get("start_time")),
        updated_at=parse_dt(raw.get("end_time") or raw.get("start_time")),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "direction": direction,
            "status": status,
            "from": from_disp,
            "to": to_disp,
            "application_id": raw.get("conversation_uuid") or raw.get("application_id"),
            "duration": raw.get("duration"),
            "price": raw.get("price"),
            "rate": raw.get("rate"),
            "network": raw.get("network"),
        },
    )
