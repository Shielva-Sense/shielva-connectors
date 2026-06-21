"""Misc utility helpers for the Aircall connector.

- `is_valid_phone` — loose E.164 validation
- `epoch_to_iso` / `iso_to_epoch` — datetime conversion
- `with_retry` — async orchestration retry shim (the HTTP client already
  retries 429/5xx; this catches unexpected transient errors that escape it,
  e.g. JSON decode flakiness from a flaky proxy).
"""
import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")

# Loose E.164 — leading +, 8-15 digits. Aircall accepts both E.164 and local-format.
_E164_RE = re.compile(r"^\+?[1-9]\d{7,14}$")


def is_valid_phone(number: str) -> bool:
    """Loose E.164 check — returns True for plausible phone numbers."""
    if not number:
        return False
    cleaned = re.sub(r"[\s\-()]", "", number)
    return bool(_E164_RE.match(cleaned))


def epoch_to_iso(epoch: Optional[int]) -> Optional[str]:
    """Convert a Unix epoch (seconds) to RFC 3339 UTC string."""
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def iso_to_epoch(iso: Optional[str]) -> Optional[int]:
    """Convert an RFC 3339 / ISO 8601 string to Unix epoch seconds."""
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx; this helper retries unexpected
    transient errors that escape the client (e.g. JSON decode flakiness on
    intermittent proxies).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
