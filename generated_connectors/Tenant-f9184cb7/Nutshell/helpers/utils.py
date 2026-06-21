"""Shared utilities for the Nutshell connector — retry helper.

The HTTP client owns its own per-request retry loop for 429 / 5xx; this
helper is for the connector layer to wrap whole-method calls that might
fail for transient reasons not surfaced as a NutshellRateLimitError.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Optional

import structlog

from exceptions import (
    NutshellNetworkError,
    NutshellRateLimitError,
)

logger = structlog.get_logger(__name__)

# OCP: change retry constants here, nowhere else.
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    coro_fn: Callable[[], Awaitable[Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> Any:
    """Execute ``coro_fn()`` with exponential-backoff retry.

    Retries on ``NutshellRateLimitError`` and ``NutshellNetworkError``.
    Re-raises the last exception after exhausting all attempts.
    """
    last_exc: BaseException = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except (NutshellRateLimitError, NutshellNetworkError) as exc:
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
            logger.info(
                "nutshell.retry",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


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
