"""Misc utility helpers for the Bitbucket connector.

`with_retry()` provides exponential backoff over the connector's coroutine
boundary — separate from the in-client httpx-level retry so the connector
can layer its own retries on `sync()` loops and other multi-step flows.

`safe_get()` walks a nested dict path defensively — used by the normalizer.
"""
import asyncio
import random
from typing import Any, Awaitable, Callable, TypeVar

import structlog

from exceptions import (
    BitbucketNetworkError,
    BitbucketRateLimitError,
    BitbucketServerError,
)

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# OCP — change here, nowhere else.
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> T:
    """Run an async callable with exponential-backoff retry.

    The HTTP client already retries 429/5xx; this helper retries the same
    typed exceptions if they bubble out (e.g. when the in-client retry
    budget is exhausted but the connector wants one more attempt across a
    `sync()` step).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except (BitbucketRateLimitError, BitbucketNetworkError, BitbucketServerError) as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            retry_after = getattr(exc, "retry_after_s", None)
            if retry_after is not None and attempt == 0:
                delay = min(retry_after, max_delay)
            else:
                delay = min(
                    base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.3),
                    max_delay,
                )
            logger.warning(
                "bitbucket.with_retry",
                attempt=attempt + 1,
                delay=delay,
                error=type(exc).__name__,
            )
            await asyncio.sleep(delay)
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely — returns `default` on any missing link."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
