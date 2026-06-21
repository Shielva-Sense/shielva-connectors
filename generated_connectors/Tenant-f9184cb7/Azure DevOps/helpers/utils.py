"""Cross-cutting utilities for the Azure DevOps connector."""
import asyncio
from typing import Any, Awaitable, Callable, Iterable, List, Optional, TypeVar

import structlog

from exceptions import (
    AzureDevOpsAuthError,
    AzureDevOpsBadRequestError,
    AzureDevOpsError,
    AzureDevOpsNotFoundError,
)

logger = structlog.get_logger(__name__)

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    base_delay: float = 0.5,
    cap: float = 8.0,
) -> T:
    """Run *fn* with exponential backoff on transient AzureDevOpsError.

    Auth, NotFound, and BadRequest errors are non-transient and re-raised
    immediately — the caller decides how to surface those. The HTTP client
    layer already retries 429/5xx with Retry-After honour; this helper exists
    for orchestration-level retries (e.g. WIQL + batch fetch).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except (
            AzureDevOpsAuthError,
            AzureDevOpsNotFoundError,
            AzureDevOpsBadRequestError,
        ):
            raise
        except AzureDevOpsError as exc:
            last_exc = exc
            if attempt + 1 >= max_retries:
                raise
            delay = min(base_delay * (2 ** attempt), cap)
            logger.warning(
                "azure_devops.with_retry.backoff",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    # Defensive — should be unreachable
    if last_exc:
        raise last_exc
    raise AzureDevOpsError("with_retry exhausted without exception")


def chunked(items: Iterable[T], size: int) -> Iterable[List[T]]:
    """Yield successive *size*-chunks from *items* (used to batch WIQL fetches)."""
    if size <= 0:
        raise ValueError("size must be positive")
    buf: List[T] = []
    for item in items:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


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
