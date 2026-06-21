"""Misc utility helpers for the Discord connector."""
import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, TypeVar

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx; this helper retries unexpected
    transient errors that escape the client (e.g. JSON decode flakiness on
    intermittent proxies).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
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


def parse_rate_limit_headers(
    headers: Dict[str, str],
) -> Tuple[Optional[int], Optional[float]]:
    """Return ``(remaining, reset_after_seconds)`` from Discord headers.

    Discord exposes ``X-RateLimit-Remaining`` (int) and
    ``X-RateLimit-Reset-After`` (float seconds). Missing values produce
    ``None`` so callers can short-circuit.
    """
    remaining: Optional[int] = None
    reset_after: Optional[float] = None
    raw_remaining = headers.get("X-RateLimit-Remaining") or headers.get(
        "x-ratelimit-remaining"
    )
    raw_reset = headers.get("X-RateLimit-Reset-After") or headers.get(
        "x-ratelimit-reset-after"
    )
    if raw_remaining is not None:
        try:
            remaining = int(raw_remaining)
        except (TypeError, ValueError):
            remaining = None
    if raw_reset is not None:
        try:
            reset_after = float(raw_reset)
        except (TypeError, ValueError):
            reset_after = None
    return remaining, reset_after
