"""Misc utility helpers for the Crisp connector."""
import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, TypeVar

from exceptions import CrispError, CrispNetworkError, CrispRateLimitError

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429 / 5xx internally; this helper retries
    unexpected transient errors that escape the client (e.g. JSON decode
    flakiness on intermittent proxies) and exhausted-retry rate-limit / 5xx
    bubbling up from the inner loop.
    """
    last_exc: Optional[Exception] = None
    delay = base_delay
    for attempt in range(max_retries):
        try:
            return await fn()
        except (CrispRateLimitError, CrispNetworkError) as exc:
            last_exc = exc
            if attempt >= max_retries - 1:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
        except CrispError as exc:
            status = getattr(exc, "status_code", 0)
            if 500 <= status < 600 and attempt < max_retries - 1:
                last_exc = exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)
                continue
            raise
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


def ts_to_dt(ts: Any) -> Optional[datetime]:
    """Crisp timestamps are ms since epoch. Defensive coerce."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return None
