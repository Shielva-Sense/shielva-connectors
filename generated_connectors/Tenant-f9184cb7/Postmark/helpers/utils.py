"""Shared retry / backoff utilities for the Postmark connector."""
import asyncio
import random
from typing import Any, Awaitable, Callable, Optional, TypeVar

import structlog

from exceptions import PostmarkError, PostmarkNetworkError, PostmarkRateLimitError

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# OCP: tune retry behaviour here, nowhere else.
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0

# Postmark uses 429 for rate limit and 5xx for transient server-side issues.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _is_retryable(exc: Exception) -> bool:
    """A PostmarkError is retryable when its status_code is in the retry set."""
    if isinstance(exc, (PostmarkNetworkError, PostmarkRateLimitError)):
        return True
    if isinstance(exc, PostmarkError):
        return getattr(exc, "status_code", 0) in RETRYABLE_STATUS_CODES
    return False


async def with_retry(
    coro_fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> T:
    """Execute ``coro_fn()`` with exponential-backoff retry on 429/5xx.

    The HTTP client raises typed exceptions; this helper decides whether a
    raised exception is transient (retry) or terminal (re-raise). On success
    returns the awaited value; on exhaustion re-raises the last exception.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt == max_retries:
                raise
            # First-attempt retry honours a server-supplied Retry-After.
            delay = retry_after if (retry_after and attempt == 0) else min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "postmark.retry",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely.

    Returns ``default`` as soon as any segment is missing or non-dict.
    Useful for Postmark payloads where camelCase nesting (e.g.
    ``Body.HTML``) varies between message-detail and list responses.
    """
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
