"""Shared utilities: retry wrapper (caller-side) — used by connector.py."""
import asyncio
import random
from typing import Any, Callable, Coroutine, Optional

import structlog

from exceptions import GrafanaNetworkError, GrafanaRateLimitError

logger = structlog.get_logger(__name__)

# OCP: retry constants — change here, nowhere else
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> Any:
    """Execute *coro_fn()* with exponential-backoff retry on rate-limit + transient network errors.

    Retries on GrafanaRateLimitError and GrafanaNetworkError. Raises the last
    exception after exhausting *max_retries*.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except GrafanaRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = retry_after if (retry_after and attempt == 0) else min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning("grafana.rate_limit — retrying", attempt=attempt + 1, delay=delay)
            await asyncio.sleep(delay)
        except GrafanaNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "grafana.network — retrying",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc
