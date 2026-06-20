from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ZoomAuthError, ZoomError, ZoomRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(prefix: str, resource_id: str) -> str:
    """Return SHA-256(prefix + resource_id)[:16] as a stable document ID."""
    raw = f"{prefix}{resource_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: ZoomError | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except ZoomAuthError:
            raise
        except ZoomRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ZoomError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def normalize_meeting(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw Zoom meeting object into a ConnectorDocument."""
    meeting_id = str(record.get("id", ""))
    topic = record.get("topic", "") or f"Meeting {meeting_id}"
    status = record.get("status", "") or ""
    start_time = record.get("start_time", "") or ""
    duration = record.get("duration", 0) or 0
    timezone = record.get("timezone", "") or ""
    host_id = record.get("host_id", "") or ""
    host_email = record.get("host_email", "") or ""
    join_url = record.get("join_url", "") or ""
    created_at = record.get("created_at", "") or ""

    title = f"Zoom meeting: {topic}"
    content_parts = [
        f"Meeting ID: {meeting_id}",
        f"Topic: {topic}",
        f"Status: {status}",
        f"Start time: {start_time}",
        f"Duration: {duration} minutes",
        f"Timezone: {timezone}",
        f"Host ID: {host_id}",
        f"Host email: {host_email}",
        f"Join URL: {join_url}",
        f"Created at: {created_at}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("meeting:", meeting_id),
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=join_url,
        metadata={
            "object_type": "meeting",
            "meeting_id": meeting_id,
            "topic": topic,
            "status": status,
            "start_time": start_time,
            "duration": duration,
            "timezone": timezone,
            "host_id": host_id,
            "host_email": host_email,
            "join_url": join_url,
        },
    )


def normalize_recording(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw Zoom cloud recording object into a ConnectorDocument."""
    meeting_id = str(record.get("id", "") or record.get("uuid", ""))
    topic = record.get("topic", "") or f"Recording {meeting_id}"
    start_time = record.get("start_time", "") or ""
    duration = record.get("duration", 0) or 0
    host_id = record.get("host_id", "") or ""
    host_email = record.get("host_email", "") or ""
    share_url = record.get("share_url", "") or ""
    recording_count = record.get("recording_count", 0) or 0
    total_size = record.get("total_size", 0) or 0

    title = f"Zoom recording: {topic}"
    content_parts = [
        f"Meeting ID: {meeting_id}",
        f"Topic: {topic}",
        f"Start time: {start_time}",
        f"Duration: {duration} minutes",
        f"Host ID: {host_id}",
        f"Host email: {host_email}",
        f"Share URL: {share_url}",
        f"Recording count: {recording_count}",
        f"Total size (bytes): {total_size}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("meeting:", meeting_id),
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=share_url,
        metadata={
            "object_type": "recording",
            "meeting_id": meeting_id,
            "topic": topic,
            "start_time": start_time,
            "duration": duration,
            "host_id": host_id,
            "host_email": host_email,
            "share_url": share_url,
            "recording_count": recording_count,
            "total_size": total_size,
        },
    )


class CircuitBreaker:
    """Simple three-state circuit breaker (closed → open → half-open → closed)."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self._failures: int = 0
        self._state: str = "closed"
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                self._state = "half-open"
        return self._state

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()

    def on_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    @property
    def is_open(self) -> bool:
        return self.state == "open"
