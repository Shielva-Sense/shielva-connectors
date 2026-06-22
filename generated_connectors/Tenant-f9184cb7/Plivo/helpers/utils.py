"""Shared utilities for the Plivo connector: retry, parameter compaction, E.164."""
from __future__ import annotations

import asyncio
import random
import re
from typing import Any, Callable, Coroutine, Dict, Optional

from exceptions import PlivoNetworkError, PlivoRateLimitError

# Retry tuning constants — change here, nowhere else.
RETRY_BASE_DELAY_S: float = 0.5
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 8.0

_E164_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


async def with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> Any:
    """Execute *coro_fn()* with exponential-backoff retry on rate-limit / network errors.

    The HTTP client already retries 429/5xx internally, so this helper is the
    second tier — it covers transient connectivity blips that surface as
    :class:`PlivoNetworkError` or :class:`PlivoRateLimitError`. Re-raises the
    last exception once *max_retries* is exhausted.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except (PlivoRateLimitError, PlivoNetworkError) as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc


def compact_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drop ``None`` values so URLs only carry caller-supplied filters.

    Plivo treats absent query keys as "no filter"; sending ``key=None`` would
    encode the literal string ``None`` and break the request. Using this helper
    everywhere keeps the call sites readable.
    """
    return {k: v for k, v in params.items() if v is not None}


def normalize_e164(number: Optional[str]) -> Optional[str]:
    """Return *number* in canonical E.164 form (leading ``+``) or ``None``.

    The Plivo API accepts numbers without a leading ``+`` but we normalize for
    consistency with the rest of the platform. Returns ``None`` if *number*
    does not look like a phone number rather than raising — the caller is
    free to validate.
    """
    if not number:
        return None
    cleaned = re.sub(r"[\s\-().]", "", number)
    if not _E164_RE.match(cleaned):
        return None
    return cleaned if cleaned.startswith("+") else "+" + cleaned
