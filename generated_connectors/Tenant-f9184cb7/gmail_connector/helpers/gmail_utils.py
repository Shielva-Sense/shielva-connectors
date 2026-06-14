"""
Pure Gmail utility functions — no HTTP, no SDK, no connector imports.

Owns the encoding concerns: building a base64url RFC822 message for send,
and base64url-decoding message part bodies for read/normalize.
"""
from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Dict, List, Optional


def build_raw_email_message(*, to: str, subject: str, body: str, sender: Optional[str] = None) -> str:
    """Build a base64url-encoded RFC822 message ready for messages.send.

    Gmail's send API expects the entire MIME message in a single base64url
    ("URL-safe, no padding stripped — Gmail accepts standard urlsafe") string.
    """
    mime = MIMEText(body, _charset="utf-8")
    mime["To"] = to
    mime["Subject"] = subject
    if sender:
        mime["From"] = sender
    raw_bytes = mime.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii")


def decode_base64url(data: str) -> str:
    """Decode a Gmail base64url body part to UTF-8 text (lossy on bad bytes)."""
    if not data:
        return ""
    # Gmail uses URL-safe base64 without padding — restore padding before decode.
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def header_value(headers: List[Dict[str, str]], name: str) -> str:
    """Return the value of the first header matching *name* (case-insensitive)."""
    lname = name.lower()
    for h in headers or []:
        if h.get("name", "").lower() == lname:
            return h.get("value", "")
    return ""


def extract_plain_text(payload: Dict) -> str:
    """Walk a Gmail message payload tree and return the best text/plain body.

    Falls back to text/html (stripped of nothing — left as-is) if no plain part,
    then to the top-level body if neither MIME part exists.
    """
    if not payload:
        return ""

    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {}) or {}
    parts = payload.get("parts", []) or []

    # Leaf node with inline data
    if not parts:
        return decode_base64url(body.get("data", ""))

    # Prefer text/plain, then text/html, recursively
    plain = _find_part_text(parts, "text/plain")
    if plain:
        return plain
    html = _find_part_text(parts, "text/html")
    if html:
        return html
    # Nothing matched — return top-level body data if present
    return decode_base64url(body.get("data", ""))


def _find_part_text(parts: List[Dict], target_mime: str) -> str:
    for part in parts:
        if part.get("mimeType") == target_mime:
            data = (part.get("body", {}) or {}).get("data", "")
            text = decode_base64url(data)
            if text:
                return text
        # Recurse into nested multiparts
        nested = part.get("parts", []) or []
        if nested:
            found = _find_part_text(nested, target_mime)
            if found:
                return found
    return ""
