"""Transforms raw Gmail API message dicts into NormalizedDocument instances."""
import base64
from typing import Any, Dict, List, Optional, Tuple

from shared.base_connector import NormalizedDocument

from helpers.utils import extract_header

# OCP-2: ordered preference list — avoids if/elif branching on MIME types
MIME_PRIORITY = ["text/plain", "text/html"]
_MIME_CONTENT_TYPE = {"text/plain": "text", "text/html": "html"}


def normalize_message(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Convert a Gmail messages.get (format=full) response to NormalizedDocument."""
    msg_id = raw.get("id", "")
    payload = raw.get("payload", {})
    headers: List[Dict[str, str]] = payload.get("headers", [])

    subject = extract_header(headers, "Subject") or "(no subject)"
    from_addr = extract_header(headers, "From")
    to_addr = extract_header(headers, "To")
    date_str = extract_header(headers, "Date")

    content, content_type = _extract_body(payload)

    return NormalizedDocument(
        id=msg_id,
        source_id=msg_id,
        title=subject,
        content=content,
        content_type=content_type,
        author=from_addr,
        metadata={
            "from": from_addr,
            "to": to_addr,
            "date": date_str,
            "labels": raw.get("labelIds", []),
            "thread_id": raw.get("threadId", ""),
            "snippet": raw.get("snippet", ""),
        },
        source="google_gmail",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def _extract_body(payload: Dict[str, Any]) -> Tuple[str, str]:
    # Iterate MIME_PRIORITY — no if/elif branching (OCP-2)
    for mime in MIME_PRIORITY:
        part = _find_part(payload, mime)
        if part is not None:
            return _decode_part(part), _MIME_CONTENT_TYPE[mime]
    # fallback: top-level body
    data = payload.get("body", {}).get("data", "")
    return _b64_decode(data), "text"


def _find_part(payload: Dict[str, Any], mime_type: str) -> Optional[Dict[str, Any]]:
    if payload.get("mimeType") == mime_type:
        return payload
    for part in payload.get("parts", []):
        found = _find_part(part, mime_type)
        if found is not None:
            return found
    return None


def _decode_part(part: Dict[str, Any]) -> str:
    data = part.get("body", {}).get("data", "")
    return _b64_decode(data)


def _b64_decode(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (4 - len(data) % 4 if len(data) % 4 else 0)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
