"""Shared utilities for the Statuspage connector — no business logic, no HTTP."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


def resolve_page_id(supplied: Optional[str], default: Optional[str]) -> str:
    """Return the page_id to use, preferring an explicit argument over the default.

    Every Statuspage REST endpoint we call is scoped under ``/pages/{id}``,
    so a missing page_id is unrecoverable — raise ``ValueError`` early.
    """
    page_id = supplied or default
    if not page_id:
        raise ValueError(
            "page_id is required — pass it as an argument or configure "
            "`page_id` at install time"
        )
    return page_id


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff.

    The HTTP client already retries 429/5xx; this helper retries unexpected
    transient errors that escape the client (e.g. JSON decode flakiness on
    intermittent proxies).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — caller-provided callable
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
