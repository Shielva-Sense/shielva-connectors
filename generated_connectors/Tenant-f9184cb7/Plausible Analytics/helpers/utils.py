"""Shared utilities for the Plausible connector: filter builder + retry shim.

The HTTP client already retries internally; ``with_retry`` is kept as a thin
shim for connector-level orchestration symmetry with the Gmail reference
connector, and so application code can compose its own retries if needed.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Iterable, List, Optional, Tuple

import structlog

from exceptions import PlausibleNetworkError, PlausibleRateLimitError

logger = structlog.get_logger(__name__)

# Defaults — change here, nowhere else (OCP)
DEFAULT_AGGREGATE_METRICS: List[str] = [
    "visitors",
    "pageviews",
    "bounce_rate",
    "visit_duration",
]
DEFAULT_TIMESERIES_METRICS: List[str] = ["visitors"]
DEFAULT_BREAKDOWN_METRICS: List[str] = ["visitors"]


def default_metrics(kind: str) -> List[str]:
    """Return the canonical default metric list for *kind*.

    Supported kinds: ``aggregate``, ``timeseries``, ``breakdown``.
    Unknown kinds fall back to the visitors-only list — keeps callers safe.
    """
    mapping = {
        "aggregate": DEFAULT_AGGREGATE_METRICS,
        "timeseries": DEFAULT_TIMESERIES_METRICS,
        "breakdown": DEFAULT_BREAKDOWN_METRICS,
    }
    return list(mapping.get(kind, DEFAULT_BREAKDOWN_METRICS))


def build_filter_string(filters: Optional[Iterable[Tuple[str, str, str]]]) -> Optional[str]:
    """Build a Plausible v1 filter expression from (property, op, value) tuples.

    Examples::

        build_filter_string([("event:page", "==", "/pricing")])
        # → 'event:page==/pricing'

        build_filter_string([
            ("event:page", "==", "/pricing"),
            ("visit:country", "==", "US"),
        ])
        # → 'event:page==/pricing;visit:country==US'

    Returns ``None`` if the iterable is empty/None so callers can pass it
    straight to ``filters=...`` without a conditional.
    """
    if not filters:
        return None
    parts: List[str] = []
    for prop, op, value in filters:
        parts.append(f"{prop}{op}{value}")
    return ";".join(parts) if parts else None


async def with_retry(
    coro_fn: Callable[[], Awaitable[Any]],
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 16.0,
) -> Any:
    """Retry an async call on rate-limit + transient network errors.

    Kept as a thin wrapper for symmetry with the Gmail connector. The HTTP
    client already retries internally, so most call sites can skip this and
    invoke the client directly; ``with_retry`` is here for callers that want
    additional connector-level resilience.
    """
    last_exc: BaseException = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except (PlausibleRateLimitError, PlausibleNetworkError) as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(base_delay * (2.0 ** attempt) + random.uniform(0, 0.25), max_delay)
            logger.warning(
                "plausible.connector.retry",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc
