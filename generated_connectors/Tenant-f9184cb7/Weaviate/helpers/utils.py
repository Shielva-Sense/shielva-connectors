"""Misc utility helpers for the Weaviate connector."""
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

    The HTTP client already retries 429 / 5xx; this helper catches transient
    errors that escape the client (e.g. JSON decode flakiness on intermittent
    proxies) at the connector orchestration layer.
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


def parse_unix_ms(value: Any) -> datetime:
    """Weaviate timestamps are unix epoch in milliseconds."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return datetime.now(timezone.utc)
    if isinstance(value, str):
        # Accept ISO strings as a fallback (some Weaviate responses include them).
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)
