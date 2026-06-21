"""Shared utilities: retry + path helpers for the Wufoo connector."""
import asyncio
import random
from typing import Any, Callable, Coroutine, Optional

import structlog

from exceptions import (
    WufooAuthError,
    WufooBadRequestError,
    WufooError,
    WufooNotFound,
)

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
    """Execute *coro_fn()* with exponential-backoff retry.

    Retries on transient WufooError (5xx / 429) — never on Auth / NotFound /
    BadRequest. Raises the last exception after exhausting all retries.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except (WufooAuthError, WufooNotFound, WufooBadRequestError):
            # Non-retryable — surface immediately.
            raise
        except WufooError as exc:
            status = getattr(exc, "status_code", 0) or 0
            # Retry only on 429 and 5xx; other 4xx surface immediately.
            retryable = status == 429 or 500 <= status < 600
            if not retryable or attempt == max_retries:
                raise
            last_exc = exc
            delay = retry_after if (retry_after and attempt == 0) else min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "wufoo.retry",
                attempt=attempt + 1,
                status=status,
                delay=delay,
            )
            await asyncio.sleep(delay)
    raise last_exc


def build_subdomain_base(subdomain: str) -> str:
    """Build a Wufoo REST base URL from the tenant's subdomain.

    Example: build_subdomain_base("acme") -> "https://acme.wufoo.com/api/v3"
    Raises ValueError on empty input.
    """
    sub = (subdomain or "").strip().lower()
    if not sub:
        raise ValueError("subdomain is required to build the Wufoo base URL")
    return f"https://{sub}.wufoo.com/api/v3"
