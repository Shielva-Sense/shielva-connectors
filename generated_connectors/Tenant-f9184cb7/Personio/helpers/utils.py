"""Shared utilities: retry harness around any async callable, chunked iterator."""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Iterable, Iterator, List, Optional, TypeVar

import structlog

from exceptions import PersonioError, PersonioNetworkError, PersonioServerError

logger = structlog.get_logger(__name__)

RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0

T = TypeVar("T")


async def with_retry(
    coro_fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> T:
    """Run *coro_fn()* with exponential-backoff retry on transient errors.

    Retries `PersonioNetworkError` (transport-level) + `PersonioServerError`
    (5xx). Auth / not-found / bad-request errors are raised immediately —
    retrying them is pointless and masks real bugs.
    """
    last_exc: Exception = RuntimeError(
        "with_retry called with max_retries=0 and no successful call"
    )
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except (PersonioNetworkError, PersonioServerError) as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = (
                retry_after
                if (retry_after and attempt == 0)
                else min(
                    base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                    max_delay,
                )
            )
            logger.warning(
                "personio.transient_error",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def chunked(iterable: Iterable[T], size: int) -> Iterator[List[T]]:
    """Yield successive chunks of *size* from *iterable*. Useful for batched sync."""
    if size <= 0:
        raise ValueError("size must be > 0")
    buf: List[T] = []
    for item in iterable:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


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
