"""Normalize GoHighLevel API resources into NormalizedDocument."""
from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            # HighLevel sometimes returns ms epoch
            ts = value / 1000.0 if value > 10_000_000_000 else float(value)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_contact(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a GoHighLevel contact into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    contact = raw.get("contact", raw) if isinstance(raw, dict) else {}
    source_id = contact.get("id", "") or contact.get("contactId", "")
    first = contact.get("firstName", "") or ""
    last = contact.get("lastName", "") or ""
    title = (
        contact.get("contactName")
        or f"{first} {last}".strip()
        or contact.get("email", "")
        or source_id
    )
    email = contact.get("email", "") or ""
    phone = contact.get("phone", "") or ""
    tags = contact.get("tags") or []
    content_parts = [title, email, phone]
    if isinstance(tags, list) and tags:
        content_parts.append("tags:" + ",".join(str(t) for t in tags))
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=" ".join(p for p in content_parts if p),
        content_type="text",
        author=email or None,
        created_at=_parse_dt(contact.get("dateAdded")),
        updated_at=_parse_dt(contact.get("dateUpdated") or contact.get("dateAdded")),
        metadata={
            "email": email,
            "phone": phone,
            "tags": tags if isinstance(tags, list) else [],
            "locationId": contact.get("locationId", ""),
            "source": contact.get("source", ""),
            "type": contact.get("type", ""),
            "kind": "gohighlevel.contact",
        },
    )


def normalize_opportunity(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a GoHighLevel opportunity into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    opp = raw.get("opportunity", raw) if isinstance(raw, dict) else {}
    source_id = opp.get("id", "")
    name = opp.get("name", "") or f"Opportunity {source_id}"
    status = opp.get("status", "") or ""
    stage = opp.get("pipelineStageId", "") or ""
    content = f"Opportunity in stage {stage} — {status}".strip()
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=content,
        content_type="text",
        created_at=_parse_dt(opp.get("createdAt")),
        updated_at=_parse_dt(opp.get("updatedAt") or opp.get("createdAt")),
        metadata={
            "status": status,
            "monetaryValue": opp.get("monetaryValue"),
            "pipelineId": opp.get("pipelineId", ""),
            "pipelineStageId": stage,
            "contactId": opp.get("contactId", ""),
            "source": opp.get("source", ""),
            "kind": "gohighlevel.opportunity",
        },
    )


def normalize_conversation(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a GoHighLevel conversation into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    conv = raw.get("conversation", raw) if isinstance(raw, dict) else {}
    source_id = conv.get("id", "")
    contact_id = conv.get("contactId", "")
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=f"Conversation {contact_id}" if contact_id else f"Conversation {source_id}",
        content=conv.get("lastMessageBody", "") or "",
        content_type="text",
        created_at=_parse_dt(conv.get("dateAdded") or conv.get("lastMessageDate")),
        updated_at=_parse_dt(conv.get("lastMessageDate")),
        metadata={
            "type": conv.get("type", ""),
            "unreadCount": conv.get("unreadCount", 0),
            "contactId": contact_id,
            "lastMessageType": conv.get("lastMessageType", ""),
            "lastMessageDate": conv.get("lastMessageDate", ""),
            "kind": "gohighlevel.conversation",
        },
    )
