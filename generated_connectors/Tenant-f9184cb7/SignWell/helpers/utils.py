"""Misc utility helpers for the SignWell connector.

The HTTP client already retries 429/5xx with exponential backoff. This module
provides:
  * `with_retry` — a higher-level retry around connector method bodies for
    transient errors that escape the client (e.g. JSON-decode flakiness on
    intermittent proxies).
  * `safe_get` — walk a nested dict path safely.
  * `validate_recipients` — guard signer payload shape before sending.
"""
import asyncio
import random
from typing import Any, Awaitable, Callable, Dict, List, TypeVar

import structlog

from exceptions import (
    SignWellNetworkError,
    SignWellRateLimitError,
    SignWellServerError,
)

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# OCP: retry constants — change here, nowhere else
RETRY_BASE_DELAY_S: float = 0.5
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 8.0


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> T:
    """Execute *fn()* with exponential-backoff retry on transient errors.

    Retries on SignWellRateLimitError, SignWellServerError, SignWellNetworkError.
    Other typed exceptions (Auth, BadRequest, NotFound, Conflict) propagate
    immediately — those represent caller errors, not transient ones.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries):
        try:
            return await fn()
        except SignWellRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                break
            delay = min(
                max(exc.retry_after_s, base_delay * (BACKOFF_FACTOR ** attempt))
                + random.uniform(0, 0.25),
                max_delay,
            )
            logger.warning(
                "signwell.with_retry.rate_limit",
                attempt=attempt + 1,
                delay=delay,
            )
            await asyncio.sleep(delay)
        except (SignWellServerError, SignWellNetworkError) as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
                max_delay,
            )
            logger.warning(
                "signwell.with_retry.transient",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely. Returns *default* on any missing segment."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def validate_recipients(recipients: List[Dict[str, Any]]) -> None:
    """Validate recipient payload shape before sending to SignWell.

    SignWell requires every recipient to have at minimum `name` + `email`. We
    fail-fast client-side with a precise message so the caller doesn't burn a
    round-trip on a 400.
    """
    if not isinstance(recipients, list) or not recipients:
        raise ValueError("recipients must be a non-empty list")
    for idx, r in enumerate(recipients):
        if not isinstance(r, dict):
            raise ValueError(f"recipients[{idx}] must be a dict")
        if not r.get("email"):
            raise ValueError(f"recipients[{idx}].email is required")
        if not r.get("name"):
            raise ValueError(f"recipients[{idx}].name is required")
