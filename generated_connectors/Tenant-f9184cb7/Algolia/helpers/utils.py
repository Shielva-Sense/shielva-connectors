"""Shared utilities: host-rotation builders + retry helper.

Algolia operates two distinct DNS pools:

  - read pool:  ``<app_id>-dsn.algolia.net`` (geo-DNS, lowest-latency PoP)
  - write pool: ``<app_id>.algolia.net``     (single primary)
  - fallback:   ``<app_id>-{1,2,3}.algolianet.com`` (separate DNS zone)

We mirror the official client's rotation strategy so a single zone outage
does not break the connector. ``build_read_hosts`` / ``build_write_hosts``
are the single owner of host rotation — never compute hosts elsewhere.
"""
import asyncio
import random
from typing import Any, Awaitable, Callable, List, Optional, TypeVar

import structlog

from exceptions import AlgoliaNetworkError, AlgoliaRateLimitError

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# OCP: retry constants — change here, nowhere else.
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


def build_read_hosts(app_id: str) -> List[str]:
    """Return the read-path host rotation for *app_id*.

    Primary: ``<app_id>-dsn.algolia.net`` (DNS-balanced across multiple regions).
    Fallback: shuffled ``<app_id>-{1,2,3}.algolianet.com`` (different DNS zone —
    survives an algolia.net outage).
    """
    if not app_id:
        raise ValueError("app_id is required to build hosts")
    primary = f"https://{app_id}-dsn.algolia.net"
    fallbacks = [
        f"https://{app_id}-1.algolianet.com",
        f"https://{app_id}-2.algolianet.com",
        f"https://{app_id}-3.algolianet.com",
    ]
    random.shuffle(fallbacks)
    return [primary, *fallbacks]


def build_write_hosts(app_id: str) -> List[str]:
    """Return the write-path host rotation for *app_id*.

    Primary: ``<app_id>.algolia.net`` (single write endpoint).
    Fallback: shuffled ``algolianet.com`` hosts (same ring as read).
    """
    if not app_id:
        raise ValueError("app_id is required to build hosts")
    primary = f"https://{app_id}.algolia.net"
    fallbacks = [
        f"https://{app_id}-1.algolianet.com",
        f"https://{app_id}-2.algolianet.com",
        f"https://{app_id}-3.algolianet.com",
    ]
    random.shuffle(fallbacks)
    return [primary, *fallbacks]


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> T:
    """Execute *fn()* with exponential-backoff retry on transient errors.

    Retries on ``AlgoliaRateLimitError`` and ``AlgoliaNetworkError`` only —
    host-fallback for in-flight 5xx is the HTTP client's job (inner loop);
    this wraps higher-level operations (sync, list_indexes, etc.) for the
    case where every host fails or the API returns 429.

    Raises the last exception after exhausting *max_retries*.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries<0")
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except AlgoliaRateLimitError as exc:
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
            logger.warning(
                "algolia.rate_limit — retrying",
                attempt=attempt + 1,
                delay=delay,
            )
            await asyncio.sleep(delay)
        except AlgoliaNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "algolia.network_error — retrying",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely. Returns *default* on any KeyError."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
