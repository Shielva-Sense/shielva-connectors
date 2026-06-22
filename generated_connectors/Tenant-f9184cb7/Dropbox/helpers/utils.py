"""Misc utility helpers for the Dropbox connector.

Owns: retry with exponential backoff, safe nested dict access, datetime
parsing, Dropbox path validators. NO HTTP, NO normalisation.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 0.5,
    **kwargs: Any,
) -> T:
    """Run an async callable with exponential backoff retry.

    Mirrors the Wix-connector helper. The HTTP client already retries 429/5xx
    and transport errors; this layer catches anything that escapes (e.g. a JSON
    decode flake on an intermittent proxy) so callers see a stable contract.

    Auth errors (raised before this layer sees them) are NOT retried.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — intentional: caller maps via exceptions.py
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely without KeyError surprises."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def parse_dt(value: Any) -> Optional[datetime]:
    """Parse a Dropbox timestamp (RFC 3339) into ``datetime``; ``None`` on failure."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def utcnow() -> datetime:
    """Timezone-aware UTC now (avoid ``datetime.utcnow()`` deprecation)."""
    return datetime.now(timezone.utc)


def normalize_dropbox_path(path: str) -> str:
    """Ensure a Dropbox path is empty (root) or starts with ``/``.

    Dropbox rejects bare path strings — the API expects ``""`` for the root or
    a leading slash for anything else.
    """
    if not path:
        return ""
    if path == "/":
        return ""
    if not path.startswith("/"):
        return "/" + path
    return path
