"""Misc utility helpers for the Qdrant connector."""
import asyncio
from typing import Any, Awaitable, Callable, TypeVar

from exceptions import QdrantAuthError, QdrantNotFound

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx; this helper retries unexpected
    transient errors that escape the client (e.g. JSON decode flakiness on
    intermittent proxies). Terminal failures — auth (401/403) and
    not-found (404) — propagate immediately because the inputs will not
    change between attempts.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except (QdrantAuthError, QdrantNotFound):
            # Terminal — do not retry.
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


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
