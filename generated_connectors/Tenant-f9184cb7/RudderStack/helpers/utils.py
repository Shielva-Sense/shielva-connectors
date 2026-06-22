"""Shared helpers for the Rudderstack connector — payload shaping, retry, ISO-8601."""
import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

T = TypeVar("T")


def iso8601_now() -> str:
    """Return current UTC time as ISO-8601 with millisecond precision + Z suffix."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def normalize_event_payload(
    user_id: str,
    extra: Dict[str, Any],
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a base Rudderstack event envelope shared across track/identify/page/group/screen/alias.

    Adds:
      - ``userId`` (required by Rudderstack / Segment-compatible spec)
      - ``timestamp`` (ISO-8601 UTC — caller-supplied or generated)
      - ``sentAt`` (always now — useful for clock-skew analysis on the server)

    Then layers ``extra`` on top so callers can attach event-type-specific keys
    (``event``, ``properties``, ``traits``, ``groupId``, ``previousId``, …).
    """
    base: Dict[str, Any] = {
        "userId": user_id,
        "timestamp": timestamp or iso8601_now(),
        "sentAt": iso8601_now(),
    }
    base.update(extra)
    return base


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx; this helper retries unexpected
    transient errors that escape the client (e.g. JSON decode flakiness on
    intermittent proxies, brief DNS failures).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely. Returns ``default`` on any miss."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
