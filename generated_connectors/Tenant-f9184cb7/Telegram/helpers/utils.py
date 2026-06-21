"""Shared utilities — Telegram-aware retry + nested dict walker."""
import asyncio
import random
from typing import Any, Awaitable, Callable, TypeVar

import httpx
import structlog

from exceptions import TelegramNetworkError, TelegramRateLimitError, TelegramServerError

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# OCP: retry constants — change here, nowhere else.
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    coro_fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> T:
    """Execute *coro_fn()* with exponential-backoff retry.

    Retries on:
    - :class:`TelegramRateLimitError` — sleeps for ``retry_after`` (Telegram
      always supplies this on 429), falling back to exponential backoff.
    - :class:`TelegramNetworkError` / :class:`TelegramServerError` /
      ``httpx.HTTPError`` — exponential backoff with jitter.

    Raises the last exception after exhausting ``max_retries`` retries.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except TelegramRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = (
                exc.retry_after
                if exc.retry_after is not None
                else min(
                    base_delay * (BACKOFF_FACTOR ** attempt)
                    + random.uniform(0, 0.5),
                    max_delay,
                )
            )
            logger.warning(
                "telegram.rate_limit — retrying",
                attempt=attempt + 1,
                delay=delay,
                retry_after=exc.retry_after,
            )
            await asyncio.sleep(delay)
        except (TelegramNetworkError, TelegramServerError, httpx.HTTPError) as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "telegram.transient_error — retrying",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely.

    ``safe_get({"a": {"b": 1}}, "a", "b")`` → ``1``;
    ``safe_get({"a": {}}, "a", "b", default=0)`` → ``0``.
    """
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
