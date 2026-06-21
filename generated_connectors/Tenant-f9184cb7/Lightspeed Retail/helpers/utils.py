"""Shared utilities: retry logic, Lightspeed list-envelope flattening, date parsing."""
import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

import structlog

from exceptions import LightspeedError, LightspeedNetworkError

logger = structlog.get_logger(__name__)

# OCP: retry constants — change here, nowhere else
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> Any:
    """Execute *coro_fn()* with exponential-backoff retry.

    Retries on LightspeedNetworkError (transient 429/5xx + transport).
    Raises the last exception after exhausting all retries.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except LightspeedNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = retry_after if (retry_after and attempt == 0) else min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "lightspeed.transient_error — retrying",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def extract_list(envelope: Dict[str, Any], resource_key: str) -> List[Dict[str, Any]]:
    """Flatten a Lightspeed list response into a Python list.

    Lightspeed wraps single-item responses as a dict, list responses as a list,
    and empty responses as an empty dict (with only "@attributes"). Normalize
    all three shapes into a plain ``List[Dict]``.
    """
    if not isinstance(envelope, dict):
        return []
    raw = envelope.get(resource_key)
    if raw is None:
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def extract_attributes(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the @attributes pagination block out of a Lightspeed envelope."""
    if not isinstance(envelope, dict):
        return {}
    attrs = envelope.get("@attributes") or {}
    return attrs if isinstance(attrs, dict) else {}


def parse_lightspeed_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Parse a Lightspeed ISO-8601-ish timestamp into an aware UTC datetime."""
    if not raw or not isinstance(raw, str):
        return None
    # Lightspeed uses RFC 3339 with optional fractional seconds: 2024-01-01T12:34:56+00:00
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
