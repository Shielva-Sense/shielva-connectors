"""Misc utility helpers for the Kommo connector."""
import asyncio
import random
from typing import Any, Awaitable, Callable, TypeVar

import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# OCP: retry constants — change here, nowhere else.
_RETRY_DELAY_S: float = 1.0
_BACKOFF_FACTOR: float = 2.0
_MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = _RETRY_DELAY_S,
    max_delay: float = _MAX_RETRY_DELAY_S,
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
            delay = min(
                base_delay * (_BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "kommo.with_retry.transient",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def sanitize_subdomain(raw: str) -> str:
    """Strip protocol + trailing ``.kommo.com`` from a user-pasted subdomain.

    Accepts ``mycompany``, ``mycompany.kommo.com``, ``https://mycompany.kommo.com/``,
    etc. Returns the bare ``mycompany`` token (lowercased, no protocol).
    """
    if not raw:
        return ""
    value = raw.strip().lower()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/", 1)[0]
    if value.endswith(".kommo.com"):
        value = value[: -len(".kommo.com")]
    return value.strip(".")


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
