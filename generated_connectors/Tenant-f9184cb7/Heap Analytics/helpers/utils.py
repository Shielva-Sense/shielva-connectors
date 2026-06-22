from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import HeapAuthError, HeapError, HeapRateLimitError
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

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour Retry-After when present.
    """
    last_exc: HeapError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except HeapAuthError:
            raise
        except HeapRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except HeapError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_user_id(identity: str) -> str:
    """Return SHA-256('user:' + identity)[:16] — stable dedup key."""
    raw = f"user:{identity}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _stable_event_id(account_id: str, event_name: str, date: str = "") -> str:
    """Return SHA-256(account_id + ':' + event_name + ':' + date)[:16]."""
    raw = f"{account_id}:{event_name}:{date}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _stable_id(raw_id: str) -> str:
    """Return SHA-256(raw_id)[:16] — for generic resource IDs."""
    return hashlib.sha256(raw_id.encode()).hexdigest()[:16]


def normalize_user(
    raw: dict[str, Any],
    account_id: str,
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a raw Heap user record into a ConnectorDocument.

    The stable source_id is SHA-256('user:' + identity)[:16].
    """
    identity: str = str(
        raw.get("identity", raw.get("user_id", raw.get("id", "")))
    )
    properties: dict[str, Any] = raw.get("properties", raw.get("user_properties", {}))
    email: str = str(properties.get("email", ""))
    name: str = str(properties.get("name", properties.get("display_name", "")))

    content_parts = [f"User identity: {identity}"]
    if email:
        content_parts.append(f"Email: {email}")
    if name:
        content_parts.append(f"Name: {name}")
    content_parts.append(f"Account ID: {account_id}")
    for key, val in properties.items():
        if key not in {"email", "name", "display_name"}:
            content_parts.append(f"{key}: {val}")

    title = f"Heap user: {email or name or identity}"

    return ConnectorDocument(
        source_id=_stable_user_id(identity) if identity else _stable_id(str(raw)),
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://heapanalytics.com/app/{account_id}/users",
        metadata={
            "resource_type": "user",
            "identity": identity,
            "email": email,
            "name": name,
            "account_id": account_id,
            "properties": properties,
        },
    )


def normalize_event(
    raw: dict[str, Any],
    account_id: str,
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a raw Heap event record into a ConnectorDocument."""
    event_name: str = str(
        raw.get("event_name", raw.get("name", raw.get("event", "unknown_event")))
    )
    count: int = int(raw.get("count", raw.get("total", 0)))
    date: str = str(raw.get("date", raw.get("time", raw.get("timestamp", ""))))
    properties: dict[str, Any] = raw.get("properties", {})

    content_parts = [
        f"Event: {event_name}",
        f"Count: {count}",
        f"Account ID: {account_id}",
    ]
    if date:
        content_parts.append(f"Date: {date}")
    for key, val in properties.items():
        content_parts.append(f"{key}: {val}")

    return ConnectorDocument(
        source_id=_stable_event_id(account_id, event_name, date),
        title=f"Heap event: {event_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://heapanalytics.com/app/{account_id}/events",
        metadata={
            "resource_type": "event",
            "event_name": event_name,
            "count": count,
            "date": date,
            "account_id": account_id,
            "properties": properties,
        },
    )


def normalize_segment(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a raw Heap segment record into a ConnectorDocument."""
    segment_id: str = str(raw.get("id", raw.get("segment_id", "")))
    name: str = str(raw.get("name", "Unnamed Segment"))
    description: str = str(raw.get("description", ""))
    count: int = int(raw.get("count", raw.get("size", raw.get("user_count", 0))))

    content_parts = [
        f"Segment ID: {segment_id}",
        f"Name: {name}",
        f"User count: {count}",
    ]
    if description:
        content_parts.append(f"Description: {description}")

    return ConnectorDocument(
        source_id=_stable_id(segment_id) if segment_id else _stable_id(name),
        title=f"Heap segment: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://heapanalytics.com/app/segments",
        metadata={
            "resource_type": "segment",
            "segment_id": segment_id,
            "name": name,
            "description": description,
            "count": count,
        },
    )
