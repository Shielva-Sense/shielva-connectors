"""Shared utilities for the Keap connector.

Currently exposes a single helper — :func:`with_retry` — that wraps an awaitable
factory with exponential-backoff retry on transient errors (HTTP 429 + network
hiccups). The retry constants are module-level so they can be tuned in one place
without touching the connector body.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Optional

import httpx

from exceptions import KeapNetworkError, KeapRateLimitError

# OCP: tune retry behavior here, never inline in connector.py.
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    coro_fn: Callable[[], Awaitable[Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> Any:
    """Execute ``coro_fn()`` with exponential-backoff retry on transient errors.

    Retries on :class:`KeapRateLimitError`, :class:`KeapNetworkError`, and
    :class:`httpx.TransportError`. Honors :attr:`KeapRateLimitError.retry_after`
    on the first attempt.

    Args:
        coro_fn:     Zero-arg factory returning a fresh awaitable each call.
        max_retries: Maximum number of retries **after** the first attempt.
        base_delay:  Initial backoff in seconds.
        max_delay:   Upper cap on any single sleep.

    Returns:
        Whatever ``coro_fn()`` returns on the first successful attempt.

    Raises:
        The last exception observed once retries are exhausted.
    """
    last_exc: BaseException = RuntimeError("with_retry called with max_retries < 0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except KeapRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = _next_delay(attempt, base_delay, max_delay, retry_after=exc.retry_after)
            await asyncio.sleep(delay)
        except KeapNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = _next_delay(attempt, base_delay, max_delay, retry_after=None)
            await asyncio.sleep(delay)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = _next_delay(attempt, base_delay, max_delay, retry_after=None)
            await asyncio.sleep(delay)
    raise last_exc


def _next_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    retry_after: Optional[float],
) -> float:
    """Pick the next backoff delay (honors ``retry_after`` on first attempt)."""
    if retry_after is not None and attempt == 0:
        return min(retry_after, max_delay)
    jitter = random.uniform(0, 0.5)
    return min(base_delay * (BACKOFF_FACTOR ** attempt) + jitter, max_delay)
