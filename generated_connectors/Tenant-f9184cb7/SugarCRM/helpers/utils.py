"""Shared utilities for the SugarCRM connector.

Two helpers live here:

* :func:`with_retry` — generic exponential-backoff retry on 429 + transport
  errors. Honors ``Retry-After`` on the first attempt.
* :func:`refresh_and_retry_on_401` — single-shot re-auth wrapper: on the first
  :class:`SugarCRMAuthError`, call the supplied ``refresh`` coroutine and retry
  the original factory exactly once.

Keeping retry policy in one module means the connector body never inlines
sleep/backoff loops, and the constants can be tuned in one place.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Optional

import httpx

from exceptions import (
    SugarCRMAuthError,
    SugarCRMNetworkError,
    SugarCRMRateLimitError,
)

# OCP: tune retry behavior here, never inline in connector.py.
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


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


async def with_retry(
    coro_fn: Callable[[], Awaitable[Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> Any:
    """Execute ``coro_fn()`` with exponential-backoff retry on transient errors.

    Retries on :class:`SugarCRMRateLimitError`, :class:`SugarCRMNetworkError`
    (which wraps transport errors and 5xx), and bare
    :class:`httpx.TransportError`. Other exceptions propagate immediately.

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
        except SugarCRMRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            await asyncio.sleep(
                _next_delay(attempt, base_delay, max_delay, retry_after=exc.retry_after)
            )
        except SugarCRMNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            await asyncio.sleep(
                _next_delay(attempt, base_delay, max_delay, retry_after=None)
            )
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            await asyncio.sleep(
                _next_delay(attempt, base_delay, max_delay, retry_after=None)
            )
    raise last_exc


async def refresh_and_retry_on_401(
    coro_fn: Callable[[], Awaitable[Any]],
    refresh: Callable[[], Awaitable[Any]],
) -> Any:
    """Call ``coro_fn()``; on 401, run ``refresh()`` and retry exactly once.

    Used by the connector to transparently recover from an expired
    SugarCRM access token. The ``refresh`` coroutine is expected to update the
    stored token in place; ``coro_fn`` is a closure that reads the latest
    token on each call.
    """
    try:
        return await coro_fn()
    except SugarCRMAuthError:
        await refresh()
        return await coro_fn()
