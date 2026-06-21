"""Shared utilities for the Bandwidth connector — pagination, date helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, Optional


_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


def parse_link_header(header_value: str) -> Dict[str, str]:
    """Parse a standard RFC 5988 Link header into {rel: url}.

    Bandwidth uses Link headers for cursor pagination on Messaging + Voice list endpoints.
    """
    if not header_value:
        return {}
    out: Dict[str, str] = {}
    for url, rel in _LINK_RE.findall(header_value):
        out[rel] = url
    return out


def extract_page_token(next_link_url: Optional[str]) -> Optional[str]:
    """Pull the `pageToken=` query value out of a Bandwidth next-link URL."""
    if not next_link_url:
        return None
    m = re.search(r"[?&]pageToken=([^&]+)", next_link_url)
    return m.group(1) if m else None


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
