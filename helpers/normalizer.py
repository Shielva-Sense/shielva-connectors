"""
Gmail Connector — Response Normalizer
SRP: All Gmail API response → NormalizedDocument transformations live here.
connector.py NEVER parses raw API responses inline.
"""
import base64
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.base_connector import NormalizedDocument


def _decode_base64url(data: str) -> str:
    """Decode a base64url-encoded string, padding as needed."""
    data = data.replace("-", "+").replace("_", "/")
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.b64decode(data).decode("utf-8", errors="replace")


def _extract_header(headers: list, name: str) -> str:
    """Extract a header value by name (case-insensitive) from a list of header dicts."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _extract_body(payload: Dict[str, Any]) -> str:
    """
    Recursively extract the best text body from a Gmail message payload.
    Preference: text/plain > text/html.
    """
    mime_type = payload.get("mimeType", "")

    # Direct body
    body_data = payload.get("body", {}).get("data", "")
    if body_data:
        return _decode_base64url(body_data)

    # Multipart: prefer text/plain part
    parts = payload.get("parts", [])
    plain_body = ""
    html_body = ""
    for part in parts:
        part_mime = part.get("mimeType", "")
        part_data = part.get("body", {}).get("data", "")
        if part_mime == "text/plain" and part_data:
            plain_body = _decode_base64url(part_data)
        elif part_mime == "text/html" and part_data:
            html_body = _decode_base64url(part_data)
        elif part_mime.startswith("multipart/"):
            # Recurse into nested multipart
            nested = _extract_body(part)
            if nested:
                plain_body = nested

    return plain_body or html_body


def normalize_message(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str,
    next_page_token: Optional[str] = None,
) -> NormalizedDocument:
    """
    Convert a raw Gmail message resource into a NormalizedDocument.

    Args:
        raw: Full Gmail message resource (format=full).
        tenant_id: Tenant identifier.
        connector_id: Connector instance identifier.
        next_page_token: Optional pagination cursor from the list response.

    Returns:
        NormalizedDocument with all required fields populated.
    """
    message_id = raw.get("id", "")
    payload = raw.get("payload", {})
    headers = payload.get("headers", [])

    subject = _extract_header(headers, "Subject") or "(no subject)"
    from_addr = _extract_header(headers, "From")
    to_addr = _extract_header(headers, "To")
    cc_addr = _extract_header(headers, "Cc")
    date_str = _extract_header(headers, "Date")
    body = _extract_body(payload) or raw.get("snippet", "")

    # Parse date string to datetime where possible
    created_at: Optional[datetime] = None
    internal_date = raw.get("internalDate")
    if internal_date:
        try:
            created_at = datetime.fromtimestamp(
                int(internal_date) / 1000, tz=timezone.utc
            )
        except (ValueError, OSError):
            pass

    metadata: Dict[str, Any] = {
        "from": from_addr,
        "to": to_addr,
        "date": date_str,
        "labels": raw.get("labelIds", []),
        "thread_id": raw.get("threadId", ""),
        "snippet": raw.get("snippet", ""),
    }
    if cc_addr:
        metadata["cc"] = cc_addr
    if next_page_token:
        metadata["next_page_token"] = next_page_token

    return NormalizedDocument(
        id=f"{tenant_id}:{connector_id}:{message_id}",
        source_id=message_id,
        title=subject,
        content=body,
        content_type="text",
        metadata=metadata,
        source="shielva_gmail",
        tenant_id=tenant_id,
        connector_id=connector_id,
        created_at=created_at,
    )
