"""Transforms raw Gmail API responses into NormalizedDocument objects."""
import base64
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import NormalizedDocument

logger = structlog.get_logger(__name__)

# OCP: MIME types tried in priority order — extend without modifying _extract_body()
MIME_PRIORITY = ["text/plain", "text/html"]

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    return _HTML_TAG_RE.sub("", html).strip()


def _decode_b64(data: str) -> str:
    """Decode a base64url-encoded Gmail body part."""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_header(headers: List[Dict[str, str]], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _decode_mime_part(mime_type: str, data: str) -> str:
    """Decode a body part according to MIME type."""
    decoded = _decode_b64(data)
    return _strip_html(decoded) if mime_type == "text/html" else decoded


def _extract_body(payload: Dict[str, Any]) -> str:
    """Recursively extract the best body text from a Gmail message payload.

    Uses MIME_PRIORITY to select content — iterate the priority list,
    avoiding if/elif chains so new MIME types can be added without editing
    the selection logic.
    """
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    # Direct single-part: check against priority list
    if body_data:
        for preferred in MIME_PRIORITY:
            if mime_type == preferred:
                return _decode_mime_part(mime_type, body_data)

    # Multipart: collect parts indexed by MIME type
    parts = payload.get("parts", [])
    collected: Dict[str, str] = {}
    for part in parts:
        part_mime = part.get("mimeType", "")
        part_data = part.get("body", {}).get("data", "")
        if part_data and part_mime in MIME_PRIORITY and part_mime not in collected:
            collected[part_mime] = _decode_mime_part(part_mime, part_data)
        elif part_mime.startswith("multipart/"):
            nested = _extract_body(part)
            if nested:
                return nested

    # Return by priority order
    for preferred in MIME_PRIORITY:
        if preferred in collected:
            return collected[preferred]

    return ""


def normalize_message(
    message: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert a full Gmail API message object to a NormalizedDocument."""
    message_id = message.get("id", "")
    payload = message.get("payload", {})
    headers = payload.get("headers", [])

    subject = _extract_header(headers, "Subject") or "(no subject)"
    from_addr = _extract_header(headers, "From") or ""
    to_addr = _extract_header(headers, "To") or ""
    cc_addr = _extract_header(headers, "Cc") or ""
    date_header = _extract_header(headers, "Date") or ""
    thread_id = message.get("threadId", "")
    label_ids = message.get("labelIds", [])
    snippet = message.get("snippet", "")
    history_id = message.get("historyId", "")

    internal_date_ms = int(message.get("internalDate", 0))
    created_at: Optional[datetime] = None
    if internal_date_ms:
        created_at = datetime.fromtimestamp(internal_date_ms / 1000, tz=timezone.utc)

    content = _extract_body(payload) or snippet

    return NormalizedDocument(
        id=f"{connector_id}_{message_id}",
        source_id=message_id,
        title=subject,
        content=content,
        content_type="text",
        source_url=f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
        author=from_addr,
        created_at=created_at,
        updated_at=created_at,
        source="google_gmail_connector",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "thread_id": thread_id,
            "label_ids": label_ids,
            "snippet": snippet,
            "from": from_addr,
            "to": to_addr,
            "cc": cc_addr,
            "date_header": date_header,
            "history_id": history_id,
        },
    )
