"""Misc utility helpers for the Shortcut connector."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx; this helper retries unexpected
    transient errors that escape the client (e.g. JSON decode flakiness on
    intermittent proxies). The last exception is re-raised on exhaustion.
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


def parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    """Parse a Shortcut ISO-8601 timestamp (trailing ``Z`` allowed)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def parse_iso8601_or_now(value: Optional[str]) -> datetime:
    """Parse an ISO-8601 timestamp; fall back to ``datetime.now(UTC)`` on failure."""
    return parse_iso8601(value) or datetime.now(timezone.utc)
