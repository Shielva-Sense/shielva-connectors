"""Misc utility helpers for the Snyk connector."""
import asyncio
from typing import Any, Awaitable, Callable, Optional, TypeVar

from exceptions import (
    SnykAuthError,
    SnykBadRequestError,
    SnykNotFoundError,
)

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    Does NOT retry on auth errors, 404s, or 400s — those are caller-actionable.
    Network / 5xx / 429 already retry inside the HTTP client; this is a thin
    outer guard for unexpected transient errors that escape it (e.g. JSON
    decode flakiness on intermittent proxies).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except (SnykAuthError, SnykBadRequestError, SnykNotFoundError):
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry exhausted retries without exception")


def parse_starting_after(next_link: str) -> Optional[str]:
    """Extract the ``starting_after`` cursor from a Snyk JSON:API next link."""
    if not next_link or "starting_after=" not in next_link:
        return None
    try:
        return next_link.split("starting_after=")[1].split("&")[0]
    except IndexError:
        return None


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
