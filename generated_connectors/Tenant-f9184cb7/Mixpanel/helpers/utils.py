"""Utility functions — event normalizer and retry helper."""
from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import MixpanelAuthError, MixpanelError, MixpanelRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors (MixpanelAuthError) are never retried — they require human
    intervention. Rate-limit errors honour the Retry-After header.
    """
    last_exc: MixpanelError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except MixpanelAuthError:
            raise  # never retry auth failures
        except MixpanelRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = (
                exc.retry_after
                if exc.retry_after > 0
                else min(
                    base_delay * (RETRY_BACKOFF_FACTOR**attempt)
                    + random.uniform(0, RETRY_JITTER_S),
                    max_delay,
                )
            )
            await asyncio.sleep(delay)
        except MixpanelError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR**attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _make_event_id(event: dict[str, Any]) -> str:
    """Build a stable 16-char hex ID for a raw Mixpanel event.

    Format (per spec): sha256("event:" + distinct_id + "_" + time)[:16]
    """
    props: dict[str, Any] = event.get("properties", {})
    distinct_id: str = str(props.get("distinct_id", ""))
    time_val: str = str(props.get("time", ""))
    raw = f"event:{distinct_id}_{time_val}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_event(event: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Mixpanel event dict (from NDJSON export) into a ConnectorDocument.

    The ``id`` field is a 16-char SHA-256 prefix:
        sha256("event:" + distinct_id + "_" + time)[:16]

    ``source`` = "mixpanel", ``type`` = "analytics_event".
    """
    props: dict[str, Any] = event.get("properties", {})
    event_name: str = str(event.get("event", "unknown"))
    distinct_id: str = str(props.get("distinct_id", ""))
    time_val: Any = props.get("time", "")
    insert_id: str = str(props.get("$insert_id", ""))
    browser: str = str(props.get("$browser", ""))
    os_val: str = str(props.get("$os", ""))
    city: str = str(props.get("$city", ""))
    country_code: str = str(props.get("mp_country_code", ""))

    doc_id = _make_event_id(event)
    title = f"Mixpanel event: {event_name}"

    content_parts: list[str] = [
        f"Event: {event_name}",
        f"Distinct ID: {distinct_id}",
        f"Timestamp: {time_val}",
    ]
    if insert_id:
        content_parts.append(f"Insert ID: {insert_id}")
    if browser:
        content_parts.append(f"Browser: {browser}")
    if os_val:
        content_parts.append(f"OS: {os_val}")
    if city:
        content_parts.append(f"City: {city}")
    if country_code:
        content_parts.append(f"Country: {country_code}")

    skip_keys = {
        "distinct_id",
        "time",
        "$insert_id",
        "$browser",
        "$os",
        "$city",
        "mp_country_code",
    }
    for key, value in props.items():
        if key not in skip_keys and value is not None:
            content_parts.append(f"{key}: {value}")

    content = "\n".join(content_parts)

    return ConnectorDocument(
        id=doc_id,
        source="mixpanel",
        type="analytics_event",
        title=title,
        content=content,
        metadata={
            "event_name": event_name,
            "distinct_id": distinct_id,
            "timestamp": time_val,
            "insert_id": insert_id,
            "browser": browser,
            "os": os_val,
            "city": city,
            "country_code": country_code,
            "raw_properties": props,
        },
    )
