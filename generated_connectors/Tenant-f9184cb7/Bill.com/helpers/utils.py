"""Misc utility helpers for the Bill.com connector.

Two concerns:

  * `with_retry` — exponential backoff retry around a transient call (network
    timeout, 5xx, 429). Auth and session errors are NOT retried here.
  * `normalize_filters` — coerce the user-facing `filters` arg (None, list, dict)
    into the Bill.com list-of-dicts shape that `data=...` expects.
  * `safe_get` — walk a nested dict path safely.
"""
import asyncio
import random
from typing import Any, Awaitable, Callable, List, Optional, TypeVar

import structlog

from exceptions import (
    BillcomNetworkError,
    BillcomRateLimitError,
)

T = TypeVar("T")

logger = structlog.get_logger(__name__)

# OCP — tune retry behaviour here, never per-call-site.
_BASE_DELAY_S: float = 0.5
_BACKOFF_FACTOR: float = 2.0
_MAX_DELAY_S: float = 8.0


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = _BASE_DELAY_S,
    max_delay: float = _MAX_DELAY_S,
) -> T:
    """Run *fn()* with exponential-backoff retry on transient errors.

    Retries on:
      - `BillcomNetworkError`  (5xx, timeouts, connection resets)
      - `BillcomRateLimitError` (429)

    Does NOT retry on:
      - `BillcomAuthError`     (operator must fix the bundle)
      - `BillcomSessionExpired` (connector layer re-logs-in)
      - `BillcomError`         (other envelope errors — bubble up)
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except (BillcomNetworkError, BillcomRateLimitError) as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            delay = min(
                base_delay * (_BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
                max_delay,
            )
            # 429 may suggest a longer wait — honour it.
            if isinstance(exc, BillcomRateLimitError) and exc.retry_after_s > delay:
                delay = min(exc.retry_after_s, max_delay)
            logger.warning(
                "billcom.with_retry",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def normalize_filters(filters: Any) -> List[dict]:
    """Coerce the user-facing `filters` arg to Bill.com's list-of-dicts shape.

    Accepts:
      - ``None``                 → ``[]``
      - ``list`` of dicts        → returned as-is
      - ``dict``                 → ``[{"field": k, "op": "=", "value": v} for k,v in dict]``

    Raises ``TypeError`` for anything else.
    """
    if not filters:
        return []
    if isinstance(filters, list):
        return filters
    if isinstance(filters, dict):
        return [{"field": k, "op": "=", "value": v} for k, v in filters.items()]
    raise TypeError(f"unsupported filters type: {type(filters).__name__}")


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely; return ``default`` on any missing key."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
