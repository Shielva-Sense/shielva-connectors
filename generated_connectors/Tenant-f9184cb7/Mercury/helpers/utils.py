"""Misc utility helpers for the Mercury connector."""
from __future__ import annotations

import asyncio
import uuid
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


def new_idempotency_key(prefix: str = "shielva-mercury") -> str:
    """Generate a stable, unique Idempotency-Key per logical operation."""
    return f"{prefix}-{uuid.uuid4()}"


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


def parse_dt(value: Any) -> datetime:
    """Best-effort parse of a value into a tz-aware datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)
