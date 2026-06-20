from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import WebexAuthError, WebexError, WebexRateLimitError
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
    last_exc: WebexError | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except WebexAuthError:
            raise
        except WebexRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except WebexError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def normalize_room(
    r: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Webex room/space object into a ConnectorDocument."""
    room_id = str(r.get("id", ""))
    title = r.get("title", "") or f"Room {room_id}"
    room_type = r.get("type", "") or ""
    created = r.get("created", "") or ""
    last_activity = r.get("lastActivity", "") or ""
    is_locked = r.get("isLocked", False)
    team_id = r.get("teamId", "") or ""

    doc_title = f"Webex room: {title}"
    content_parts = [
        f"Room ID: {room_id}",
        f"Title: {title}",
        f"Type: {room_type}",
        f"Created: {created}",
        f"Last activity: {last_activity}",
        f"Locked: {is_locked}",
        f"Team ID: {team_id}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("room:", room_id),
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "room",
            "room_id": room_id,
            "title": title,
            "type": room_type,
            "created": created,
            "last_activity": last_activity,
            "is_locked": is_locked,
            "team_id": team_id,
        },
    )


def normalize_meeting(
    m: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Webex meeting object into a ConnectorDocument."""
    meeting_id = str(m.get("id", ""))
    meeting_title = m.get("title", "") or f"Meeting {meeting_id}"
    start = m.get("start", "") or ""
    end = m.get("end", "") or ""
    timezone = m.get("timezone", "") or ""
    meeting_type = m.get("meetingType", "") or ""
    status = m.get("status", "") or ""
    host_email = m.get("hostEmail", "") or ""
    web_link = m.get("webLink", "") or ""

    doc_title = f"Webex meeting: {meeting_title}"
    content_parts = [
        f"Meeting ID: {meeting_id}",
        f"Title: {meeting_title}",
        f"Start: {start}",
        f"End: {end}",
        f"Timezone: {timezone}",
        f"Type: {meeting_type}",
        f"Status: {status}",
        f"Host email: {host_email}",
        f"Web link: {web_link}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("meeting:", meeting_id),
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=web_link,
        metadata={
            "object_type": "meeting",
            "meeting_id": meeting_id,
            "title": meeting_title,
            "start": start,
            "end": end,
            "timezone": timezone,
            "meeting_type": meeting_type,
            "status": status,
            "host_email": host_email,
            "web_link": web_link,
        },
    )


def normalize_message(
    msg: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Webex message object into a ConnectorDocument."""
    message_id = str(msg.get("id", ""))
    room_id = msg.get("roomId", "") or ""
    text = msg.get("text", "") or msg.get("markdown", "") or ""
    person_email = msg.get("personEmail", "") or ""
    created = msg.get("created", "") or ""
    room_type = msg.get("roomType", "") or ""

    doc_title = f"Webex message by {person_email}" if person_email else f"Webex message {message_id}"
    content_parts = [
        f"Message ID: {message_id}",
        f"Room ID: {room_id}",
        f"From: {person_email}",
        f"Created: {created}",
        f"Room type: {room_type}",
        f"Text: {text}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("message:", message_id),
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "message",
            "message_id": message_id,
            "room_id": room_id,
            "person_email": person_email,
            "created": created,
            "room_type": room_type,
            "text": text,
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
