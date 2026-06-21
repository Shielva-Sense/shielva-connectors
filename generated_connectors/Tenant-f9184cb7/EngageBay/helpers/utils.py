"""Generic helpers reused across the EngageBay connector."""
import asyncio
import random
from typing import Any, Awaitable, Callable, Optional

import structlog

from exceptions import EngageBayAuthError, EngageBayError, EngageBayNotFound

logger = structlog.get_logger(__name__)


async def with_retry(
    func: Callable[[], Awaitable[Any]],
    *,
    max_retries: int = 3,
    base_delay: float = 0.5,
    cap: float = 8.0,
) -> Any:
    """Retry an async callable on transient errors.

    Auth and not-found errors are non-retryable — they propagate immediately.
    All other EngageBayError instances retry with exponential backoff + jitter.
    The HTTP client already handles 429/5xx; this is a defensive outer layer
    for callers that want a second retry budget.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await func()
        except (EngageBayAuthError, EngageBayNotFound):
            raise
        except EngageBayError as exc:
            last_exc = exc
            if attempt + 1 >= max_retries:
                raise
            delay = min(cap, base_delay * (2 ** attempt)) + random.uniform(0, 0.25)
            logger.info("engagebay.with_retry", attempt=attempt + 1, delay=delay)
            await asyncio.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise EngageBayError("with_retry: exhausted with no captured error")


def require(value: Any, name: str) -> Any:
    """Raise ValueError when *value* is None / empty string."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{name} is required")
    return value
