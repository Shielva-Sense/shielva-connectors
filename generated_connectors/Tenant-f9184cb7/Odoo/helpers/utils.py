"""Shared utilities: retry wrapper for transient Odoo failures."""
import asyncio
import random
from typing import Any, Awaitable, Callable

from exceptions import OdooNetworkError

_BACKOFF_BASE_S = 0.5
_BACKOFF_FACTOR = 2.0
_BACKOFF_MAX_S = 30.0


async def with_retry(
    coro_fn: Callable[..., Awaitable[Any]],
    *args: Any,
    max_retries: int = 3,
    **kwargs: Any,
) -> Any:
    """Execute ``coro_fn(*args, **kwargs)`` with exponential-backoff retry.

    Retries on :class:`exceptions.OdooNetworkError` only. Auth / access errors
    are NOT retried because the credentials will not change between attempts.
    Raises the last exception after exhausting all retries.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=-1")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except OdooNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                _BACKOFF_BASE_S * (_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, 0.25),
                _BACKOFF_MAX_S,
            )
            await asyncio.sleep(delay)
    raise last_exc
