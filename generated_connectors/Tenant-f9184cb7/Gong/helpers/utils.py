from __future__ import annotations

import asyncio
import hashlib
import random
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from exceptions import GongAuthError, GongError, GongRateLimitError
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
    last_exc: GongError | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except GongAuthError:
            raise
        except GongRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except GongError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def normalize_call(c: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Gong call object into a ConnectorDocument.

    id  = SHA-256("call:" + str(c["id"]))[:16]
    type = "call"
    """
    call_id = str(c.get("id", ""))
    title = c.get("title", "") or f"Gong call {call_id}"
    duration = c.get("duration", 0) or 0
    started = c.get("started", "") or c.get("startTime", "") or ""
    url = c.get("url", "") or ""

    # Participants / parties
    parties: list[dict[str, Any]] = c.get("parties", []) or []
    party_names = [
        p.get("name", "") or p.get("emailAddress", "") or ""
        for p in parties
    ]
    party_str = ", ".join(n for n in party_names if n)

    content_parts = [
        f"Call ID: {call_id}",
        f"Title: {title}",
        f"Started: {started}",
        f"Duration: {duration} seconds",
        f"Participants: {party_str}" if party_str else "",
        f"URL: {url}" if url else "",
    ]

    return ConnectorDocument(
        source_id=_stable_id("call:", call_id),
        title=f"Gong call: {title}",
        content="\n".join(p for p in content_parts if p),
        connector_id="",
        tenant_id="",
        source_url=url,
        metadata={
            "type": "call",
            "call_id": call_id,
            "title": title,
            "duration": duration,
            "started": started,
            "parties": parties,
        },
    )


def normalize_user(u: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Gong user object into a ConnectorDocument.

    id  = SHA-256("user:" + str(u["id"]))[:16]
    type = "user"
    """
    user_id = str(u.get("id", ""))
    name = u.get("name", "") or u.get("firstName", "") or f"User {user_id}"
    email = u.get("emailAddress", "") or u.get("email", "") or ""
    title_field = u.get("title", "") or ""
    manager_id = str(u.get("managerId", "") or "")

    content_parts = [
        f"User ID: {user_id}",
        f"Name: {name}",
        f"Email: {email}" if email else "",
        f"Title: {title_field}" if title_field else "",
        f"Manager ID: {manager_id}" if manager_id else "",
    ]

    return ConnectorDocument(
        source_id=_stable_id("user:", user_id),
        title=f"Gong user: {name}",
        content="\n".join(p for p in content_parts if p),
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "type": "user",
            "user_id": user_id,
            "name": name,
            "email": email,
            "title": title_field,
            "manager_id": manager_id,
        },
    )


def normalize_transcript(
    t: dict[str, Any], call_id: str
) -> ConnectorDocument:
    """Convert a raw Gong transcript object into a ConnectorDocument.

    id  = SHA-256("transcript:" + str(call_id))[:16]
    type = "transcript"
    """
    # Gong transcript shape: {callId, transcript: [{speakerId, topic, sentences: [{start, end, text}]}]}
    transcript_segments: list[dict[str, Any]] = t.get("transcript", []) or []
    lines: list[str] = []
    for segment in transcript_segments:
        speaker = segment.get("speakerId", "") or "Unknown"
        for sentence in segment.get("sentences", []) or []:
            text = sentence.get("text", "") or ""
            if text:
                lines.append(f"{speaker}: {text}")

    content = "\n".join(lines) if lines else f"Transcript for call {call_id}"

    return ConnectorDocument(
        source_id=_stable_id("transcript:", str(call_id)),
        title=f"Gong transcript: call {call_id}",
        content=content,
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "type": "transcript",
            "call_id": call_id,
            "segment_count": len(transcript_segments),
        },
    )
