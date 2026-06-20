from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import FullStoryAuthError, FullStoryError, FullStoryRateLimitError
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
    last_exc: FullStoryError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except FullStoryAuthError:
            raise
        except FullStoryRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except FullStoryError as exc:
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


def _stable_id(raw: str) -> str:
    """Return SHA-256(raw)[:16] — generic stable dedup key."""
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_session(
    s: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a raw FullStory session recording into a ConnectorDocument.

    The stable source_id is SHA-256('session:' + s['id'])[:16].
    """
    session_id: str = str(s.get("id", s.get("sessionId", s.get("session_id", ""))))
    uid: str = str(s.get("uid", s.get("userId", s.get("user_id", ""))))
    created_time: str = str(s.get("createdTime", s.get("created_time", s.get("startTime", ""))))
    duration_ms: int = int(s.get("durationMs", s.get("duration_ms", s.get("duration", 0))))
    page_url: str = str(s.get("pageUrl", s.get("page_url", s.get("url", ""))))

    content_parts = [f"Session ID: {session_id}"]
    if uid:
        content_parts.append(f"User ID: {uid}")
    if created_time:
        content_parts.append(f"Created: {created_time}")
    if duration_ms:
        content_parts.append(f"Duration (ms): {duration_ms}")
    if page_url:
        content_parts.append(f"Page URL: {page_url}")

    return ConnectorDocument(
        source_id=_stable_id(f"session:{session_id}") if session_id else _stable_id(str(s)),
        title=f"FullStory session: {session_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.fullstory.com/ui/sessions/{session_id}" if session_id else "",
        metadata={
            "resource_type": "session_recording",
            "session_id": session_id,
            "uid": uid,
            "created_time": created_time,
            "duration_ms": duration_ms,
            "page_url": page_url,
        },
    )


def normalize_user(
    u: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a raw FullStory user record into a ConnectorDocument.

    The stable source_id is SHA-256('user:' + u['uid'])[:16].
    """
    uid: str = str(u.get("uid", u.get("userId", u.get("id", ""))))
    display_name: str = str(u.get("displayName", u.get("display_name", u.get("name", ""))))
    email: str = str(u.get("email", ""))
    properties: dict[str, Any] = u.get("properties", u.get("userProperties", {}))

    content_parts = [f"User UID: {uid}"]
    if display_name:
        content_parts.append(f"Display Name: {display_name}")
    if email:
        content_parts.append(f"Email: {email}")
    for key, val in properties.items():
        if key not in {"displayName", "display_name", "email", "name"}:
            content_parts.append(f"{key}: {val}")

    title = f"FullStory user: {email or display_name or uid}"

    return ConnectorDocument(
        source_id=_stable_id(f"user:{uid}") if uid else _stable_id(str(u)),
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.fullstory.com/ui/users/{uid}" if uid else "",
        metadata={
            "resource_type": "user",
            "uid": uid,
            "display_name": display_name,
            "email": email,
            "properties": properties,
        },
    )


def normalize_segment(
    s: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a raw FullStory segment record into a ConnectorDocument.

    The stable source_id is SHA-256('segment:' + s['id'])[:16].
    """
    segment_id: str = str(s.get("id", s.get("segmentId", s.get("segment_id", ""))))
    name: str = str(s.get("name", "Unnamed Segment"))
    description: str = str(s.get("description", ""))
    count: int = int(s.get("count", s.get("memberCount", s.get("size", 0))))

    content_parts = [
        f"Segment ID: {segment_id}",
        f"Name: {name}",
        f"Member count: {count}",
    ]
    if description:
        content_parts.append(f"Description: {description}")

    return ConnectorDocument(
        source_id=_stable_id(f"segment:{segment_id}") if segment_id else _stable_id(name),
        title=f"FullStory segment: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://app.fullstory.com/ui/segments",
        metadata={
            "resource_type": "segment",
            "segment_id": segment_id,
            "name": name,
            "description": description,
            "count": count,
        },
    )
