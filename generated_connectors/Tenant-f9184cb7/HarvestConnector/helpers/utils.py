"""Misc utility helpers for the Harvest connector."""
import asyncio
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Optional, TypeVar, Union

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
    last_exc: Exception | None = None
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


def iso_date(value: Union[str, date, datetime, None]) -> Optional[str]:
    """Return Harvest's `YYYY-MM-DD` date format, or None.

    Harvest's `from`/`to` parameters and the `spent_date` field use this exact
    shape — never with a time component.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        # Trust callers to pass YYYY-MM-DD; accept ISO 8601 and trim.
        if "T" in value:
            return value.split("T", 1)[0]
        return value
    raise TypeError(f"Unsupported date value: {type(value).__name__}")
