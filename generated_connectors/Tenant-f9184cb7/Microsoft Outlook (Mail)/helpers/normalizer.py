"""Normalize a Microsoft Graph message into a Shielva NormalizedDocument."""
from __future__ import annotations

from typing import Any, Dict, List

from shared.base_connector import NormalizedDocument


def _extract_body_text(raw: Dict[str, Any]) -> str:
    body = raw.get("body") or {}
    return body.get("content") or raw.get("bodyPreview") or ""


def _extract_addresses(field: List[Dict[str, Any]] | None) -> List[str]:
    if not field:
        return []
    out: List[str] = []
    for entry in field:
        addr = (entry.get("emailAddress") or {}).get("address")
        if addr:
            out.append(addr)
    return out


def normalize_message(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert a Graph ``message`` resource into a NormalizedDocument."""
    message_id = raw.get("id", "")
    subject = raw.get("subject") or "(no subject)"
    from_addr = (
        (raw.get("from") or {}).get("emailAddress", {}).get("address", "")
    )
    to_addrs = _extract_addresses(raw.get("toRecipients"))
    cc_addrs = _extract_addresses(raw.get("ccRecipients"))
    received_at = raw.get("receivedDateTime") or raw.get("sentDateTime") or ""
    body_text = _extract_body_text(raw)

    body = raw.get("body") or {}
    content_type = (body.get("contentType") or "text").lower()
    if content_type not in ("text", "html", "markdown", "pdf"):
        content_type = "text"

    return NormalizedDocument(
        id=message_id,
        source_id=message_id,
        title=subject,
        content=body_text,
        content_type=content_type,
        source_url=raw.get("webLink") or None,
        url=raw.get("webLink") or None,
        author=from_addr or None,
        source="outlook_mail",
        connector_id=connector_id,
        tenant_id=tenant_id,
        metadata={
            "from": from_addr,
            "to": to_addrs,
            "cc": cc_addrs,
            "received_at": received_at,
            "conversation_id": raw.get("conversationId", ""),
            "is_read": raw.get("isRead", False),
            "has_attachments": raw.get("hasAttachments", False),
            "web_link": raw.get("webLink", ""),
        },
    )
