"""Shared utilities for the Attio connector.

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
import structlog

from exceptions import AttioRateLimitError, AttioServerError

logger = structlog.get_logger(__name__)

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

    Retries on :class:`AttioRateLimitError`, :class:`AttioServerError`, and
    :class:`httpx.TransportError`. Honors :attr:`AttioRateLimitError.retry_after_s`
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
        except AttioRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            retry_after = getattr(exc, "retry_after_s", None) or getattr(exc, "retry_after", None)
            delay = _next_delay(attempt, base_delay, max_delay, retry_after=retry_after)
            logger.warning(
                "attio.rate_limit — retrying",
                attempt=attempt + 1,
                delay=delay,
            )
            await asyncio.sleep(delay)
        except AttioServerError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = _next_delay(attempt, base_delay, max_delay, retry_after=None)
            logger.warning(
                "attio.server_error — retrying",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = _next_delay(attempt, base_delay, max_delay, retry_after=None)
            logger.warning(
                "attio.transport_error — retrying",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
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
        return min(float(retry_after), max_delay)
    jitter = random.uniform(0, 0.5)
    return min(base_delay * (BACKOFF_FACTOR ** attempt) + jitter, max_delay)


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
