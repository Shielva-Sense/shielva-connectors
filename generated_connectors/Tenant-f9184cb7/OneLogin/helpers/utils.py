"""Shared utilities for the OneLogin connector.

Owns:
- ``compute_base_url(subdomain)`` — resolves the per-tenant OneLogin root URL.
- ``with_retry()`` — exponential backoff for unexpected transient errors that
  escape the HTTP client (e.g. JSON decode flakiness on intermittent proxies).
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Optional, TypeVar

import httpx
import structlog

from exceptions import OneLoginNetworkError, OneLoginRateLimitError

logger = structlog.get_logger(__name__)

RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0

T = TypeVar("T")


async def with_retry(
    coro_fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> T:
    """Run *coro_fn* with exponential-backoff retry on 429 + transient transport errors.

    Retries on:
      - ``OneLoginRateLimitError`` (HTTP 429)
      - ``OneLoginNetworkError`` (HTTP 5xx + transport-level)
      - ``httpx.RequestError`` subclasses

    Raises the last exception once *max_retries* attempts are exhausted.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except OneLoginRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            ra = exc.retry_after_s if hasattr(exc, "retry_after_s") else None
            delay = (
                ra
                if (ra and attempt == 0)
                else min(
                    base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                    max_delay,
                )
            )
            logger.warning(
                "onelogin.rate_limit.retry",
                attempt=attempt + 1,
                delay=delay,
            )
            await asyncio.sleep(delay)
        except (OneLoginNetworkError, httpx.RequestError) as exc:
            last_exc = (
                exc if isinstance(exc, Exception) else OneLoginNetworkError(str(exc))
            )
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "onelogin.transport.retry",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def compute_base_url(subdomain: str) -> str:
    """Resolve the per-tenant OneLogin root URL from a subdomain.

    OneLogin tenants are addressed as ``{subdomain}.onelogin.com``. The
    returned URL is the root (no path) — the HTTP client appends
    ``/api/2/<path>`` and ``/auth/oauth2/v2/token`` per call.

    Accepts:
      - bare subdomain: ``acme``
      - full URL: ``https://acme.onelogin.com``
      - dotted form: ``acme.onelogin.com``
    """
    sub = (subdomain or "").strip().lower()
    for prefix in ("https://", "http://"):
        if sub.startswith(prefix):
            sub = sub[len(prefix) :]
    sub = sub.split(".onelogin.com")[0].rstrip("/")
    if not sub:
        raise ValueError("subdomain is required to compute the OneLogin base URL")
    logger.debug("onelogin.base_url.resolved", subdomain=sub)
    return f"https://{sub}.onelogin.com"


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely; return ``default`` if any segment is missing."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
