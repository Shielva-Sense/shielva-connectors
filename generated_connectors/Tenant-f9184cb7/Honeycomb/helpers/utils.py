"""Shared utilities for the Honeycomb connector — slugify + orchestration retry.

`HoneycombHTTPClient` already retries 429/5xx internally. This module's
`with_retry` is for orchestration-layer retries (multi-step workflows
where a single failure should not abort the whole sync run).
"""
import asyncio
import random
import re
from typing import Any, Awaitable, Callable, Optional, TypeVar

import structlog

from exceptions import HoneycombNetworkError, HoneycombRateLimitError

logger = structlog.get_logger(__name__)

# OCP — retry tunables, change here, nowhere else
_BASE_DELAY_S = 0.5
_BACKOFF_FACTOR = 2.0
_MAX_DELAY_S = 16.0

# Honeycomb dataset slugs are URL-safe: lowercase alphanumerics + hyphens
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9-]+")
_SLUG_COLLAPSE_RE = re.compile(r"-+")

T = TypeVar("T")


def slugify(value: str) -> str:
    """Normalize a human-readable name into a Honeycomb-style slug.

    Honeycomb's API segments include the dataset slug in the URL path; sending
    a name with whitespace or special characters will 404. This helper lowers,
    hyphenates, strips disallowed characters and collapses runs of hyphens.
    """
    if not value:
        return ""
    lowered = value.strip().lower().replace(" ", "-")
    stripped = _SLUG_STRIP_RE.sub("-", lowered)
    collapsed = _SLUG_COLLAPSE_RE.sub("-", stripped)
    return collapsed.strip("-")


async def with_retry(
    coro_fn: Callable[[], Awaitable[T]],
    max_retries: int = 2,
    base_delay: float = _BASE_DELAY_S,
    max_delay: float = _MAX_DELAY_S,
    retry_after: Optional[float] = None,
) -> T:
    """Run an async callable with exponential-backoff retry on transient errors.

    Retries on HoneycombRateLimitError and HoneycombNetworkError. The underlying
    HoneycombHTTPClient already retries 429 / 5xx internally; this helper is
    layered on top for multi-step workflow orchestration.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except HoneycombRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = retry_after if (retry_after and attempt == 0) else min(
                base_delay * (_BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
                max_delay,
            )
            logger.warning("honeycomb.rate_limit_retry", attempt=attempt + 1, delay=delay)
            await asyncio.sleep(delay)
        except HoneycombNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (_BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
                max_delay,
            )
            logger.warning(
                "honeycomb.network_retry",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely without raising on missing keys."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
