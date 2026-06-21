"""Shared utilities for the HiBob connector: retry + humaniser."""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")

# OCP: retry constants — change here, nowhere else.
RETRY_DELAY_S: float = 0.5
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 16.0


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> T:
    """Run an async callable with exponential-backoff retry.

    The HTTP client already retries 429/5xx internally; this helper is a
    generic safety net for transient blips at the orchestration layer (e.g.
    JSON decode flakiness on intermittent proxies).
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — caller decides the retry surface
            last_exc = exc
            if attempt == max_retries:
                break
            delay = (
                retry_after
                if (retry_after and attempt == 0)
                else min(
                    base_delay * (BACKOFF_FACTOR ** attempt)
                    + random.uniform(0, 0.25),
                    max_delay,
                )
            )
            await asyncio.sleep(delay)
    raise last_exc


def humanize_employee_fields(raw: dict, include: Optional[list] = None) -> dict:
    """Return a tidy ``label: value`` mapping for the requested employee fields.

    HiBob's responses are deeply nested. When callers ask for a human view
    they get a flat dict keyed by the field's display label.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    include_set = set(include or [])
    for key, value in raw.items():
        if include_set and key not in include_set:
            continue
        label = str(key).replace("_", " ").replace(".", " > ").title()
        out[label] = value
    return out


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
