"""Misc utility helpers for the Sage Intacct connector.

Sage Intacct's XML gateway uses HTTP 200 even for application-level failures
(the failure is buried in ``<status>failure</status>`` inside the envelope),
so the connector-level retry triggers off ``SageIntacctNetworkError`` only.
The HTTP client retries 429 / 5xx / transport errors itself — this helper
is for the connector layer to absorb occasional transport blips during
multi-call sync loops.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Optional, TypeVar

from exceptions import SageIntacctNetworkError

T = TypeVar("T")

RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> T:
    """Run an async callable with exponential-backoff retry.

    Only retries on :class:`SageIntacctNetworkError` — auth, validation and
    not-found errors fail fast. The HTTP client already retries 429/5xx;
    this helper handles transient failures that escape the client.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except SageIntacctNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = (
                retry_after
                if (retry_after and attempt == 0)
                else min(
                    base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                    max_delay,
                )
            )
            await asyncio.sleep(delay)
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
