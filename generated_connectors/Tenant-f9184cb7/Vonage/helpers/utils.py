"""Shared utilities for the Vonage connector — auth headers, JWT mint, pagination, dates."""

from __future__ import annotations

import base64
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import jwt


# ── HTTP Basic header ────────────────────────────────────────────────────────


def basic_auth_header(api_key: str, api_secret: str) -> str:
    """Return the value of an HTTP Basic Authorization header for `api_key:api_secret`."""
    raw = f"{api_key}:{api_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ── JWT mint (Voice / Messages / Conversations) ──────────────────────────────


def mint_vonage_jwt(
    application_id: str,
    private_key_pem: str,
    *,
    ttl_seconds: int = 60,
) -> str:
    """Mint a short-lived RS256 JWT for the Vonage Voice / Messages API.

    Spec: https://developer.vonage.com/getting-started/concepts/authentication#jwts

    Args:
        application_id: Vonage application UUID.
        private_key_pem: RSA private key in PEM form.
        ttl_seconds: JWT lifetime (default 60s — Vonage rejects > 24h).

    Returns:
        The signed JWT string.
    """
    now = int(time.time())
    payload: Dict[str, Any] = {
        "iat": now,
        "jti": str(uuid.uuid4()),
        "exp": now + ttl_seconds,
        "application_id": application_id,
    }
    token = jwt.encode(payload, private_key_pem, algorithm="RS256")
    # PyJWT >= 2.0 returns str. Older versions return bytes. Normalise.
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token


# ── Pagination helpers (RFC 5988 Link headers + Vonage `_links.next`) ────────


_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')


def parse_link_header(header_value: str) -> Dict[str, str]:
    """Parse a standard RFC 5988 Link header into {rel: url}."""
    if not header_value:
        return {}
    out: Dict[str, str] = {}
    for url, rel in _LINK_RE.findall(header_value):
        out[rel] = url
    return out


def extract_record_index(next_link_url: Optional[str]) -> Optional[int]:
    """Pull `record_index` (0-based) from a Vonage Voice next-link URL."""
    if not next_link_url:
        return None
    m = re.search(r"[?&]record_index=([0-9]+)", next_link_url)
    return int(m.group(1)) if m else None


def extract_page_index(next_link_url: Optional[str]) -> Optional[int]:
    """Pull `page_index` from a Vonage SMS-search next-link URL."""
    if not next_link_url:
        return None
    m = re.search(r"[?&]page_index=([0-9]+)", next_link_url)
    return int(m.group(1)) if m else None


# ── ISO-8601 helpers ─────────────────────────────────────────────────────────


def parse_dt(raw: Any) -> Optional[datetime]:
    """Parse an arbitrary Vonage timestamp into a tz-aware datetime."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw)
    # Vonage uses both ISO-8601 with Z and a `YYYY-MM-DD HH:MM:SS` space form.
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Fall back to `YYYY-MM-DD HH:MM:SS`
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialise a datetime to RFC 3339 with explicit UTC tz."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
