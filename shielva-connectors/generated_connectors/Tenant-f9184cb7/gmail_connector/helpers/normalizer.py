"""
Transform a raw Gmail message resource into a NormalizedDocument.
Pure transformation — no HTTP, no connector imports.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from shared.base_connector import NormalizedDocument

from helpers.gmail_utils import extract_plain_text, header_value


def normalize_message(
    raw: dict,
    *,
    tenant_id: str,
    connector_id: str,
    source: str = "gmail",
) -> NormalizedDocument:
    """Map a Gmail messages.get(format=full) response to a NormalizedDocument."""
    msg_id = raw.get("id", "")
    payload = raw.get("payload", {}) or {}
    headers = payload.get("headers", []) or []

    subject = header_value(headers, "Subject") or "(no subject)"
    sender = header_value(headers, "From")
    snippet = raw.get("snippet", "")
    body = extract_plain_text(payload) or snippet

    created_at = _parse_internal_date(raw.get("internalDate"))

    # Tenant-scoped, stable document id so re-ingest is idempotent per tenant.
    doc_id = f"{tenant_id}:gmail:{msg_id}"

    return NormalizedDocument(
        id=doc_id,
        source_id=msg_id,
        title=subject,
        content=body,
        content_type="text",
        source_url=f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
        url=f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
        author=sender,
        created_at=created_at,
        updated_at=created_at,
        source=source,
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "thread_id": raw.get("threadId", ""),
            "label_ids": raw.get("labelIds", []),
            "from": sender,
            "to": header_value(headers, "To"),
            "date": header_value(headers, "Date"),
            "snippet": snippet,
        },
    )


def _parse_internal_date(internal_date: Optional[str]) -> Optional[datetime]:
    """Gmail internalDate is epoch milliseconds as a string."""
    if not internal_date:
        return None
    try:
        return datetime.fromtimestamp(int(internal_date) / 1000.0, tz=timezone.utc)
    except (ValueError, TypeError):
        return None
