"""Shared utilities: retry, query-param helpers."""
import asyncio
import random
from typing import Any, Callable, Coroutine, Dict, Optional

import structlog

from exceptions import ClockifyNetworkError, ClockifyRateLimitError

logger = structlog.get_logger(__name__)

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
    """Execute *coro_fn()* with exponential-backoff retry on 429 / network errors."""
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except ClockifyRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = retry_after if (retry_after and attempt == 0) else min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning("clockify.rate_limit_retry", attempt=attempt + 1, delay=delay)
            await asyncio.sleep(delay)
        except ClockifyNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "clockify.network_retry",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def build_paged_params(
    page: int = 1,
    page_size: int = 50,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a Clockify query-param dict with `page` + `page-size` (Clockify uses hyphen)."""
    params: Dict[str, Any] = {"page": int(page), "page-size": int(page_size)}
    if extra:
        for k, v in extra.items():
            if v is None:
                continue
            params[k] = v
    return params
