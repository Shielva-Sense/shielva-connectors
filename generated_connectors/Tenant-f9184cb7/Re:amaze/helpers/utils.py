from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ReamazeAuthError, ReamazeError, ReamazeRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(value: str) -> str:
    """Return a 16-character hex digest (sha256 prefix) for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_conversation(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Re:amaze conversation into a ConnectorDocument.

    Stable source_id: sha256("conversation:" + slug)[:16]
    """
    slug: str = raw.get("slug", "") or str(raw.get("id", ""))
    subject: str = raw.get("subject", "") or f"Conversation {slug}"
    status: str = str(raw.get("status", "")) or "open"
    channel: str = raw.get("channel", {}).get("name", "") if isinstance(raw.get("channel"), dict) else ""
    created_at: str = raw.get("created_at", "") or ""
    updated_at: str = raw.get("updated_at", "") or ""
    tags: list[str] = [t.get("name", "") for t in raw.get("tags", []) if isinstance(t, dict)]

    # Build content from messages if present
    content_parts: list[str] = [f"Subject: {subject}"]
    messages: list[dict[str, Any]] = raw.get("messages", []) or []
    for msg in messages:
        body: str = msg.get("body", "") or ""
        author: str = ""
        author_obj = msg.get("author", {})
        if isinstance(author_obj, dict):
            author = author_obj.get("email", "") or author_obj.get("name", "") or ""
        if body:
            header = f"Message from {author}:" if author else "Message:"
            content_parts.append(f"{header}\n{body}")

    content = "\n\n".join(content_parts) if content_parts else subject

    source_id = _short_id(f"conversation:{slug}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Conversation: {subject}",
        content=content,
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "slug": slug,
            "status": status,
            "channel": channel,
            "tags": tags,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_contact(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Re:amaze contact/person into a ConnectorDocument.

    Stable source_id: sha256("contact:" + str(id))[:16]
    """
    contact_id: int = raw.get("id", 0)
    name: str = raw.get("name", "") or f"Contact #{contact_id}"
    email: str = raw.get("email", "") or ""
    phone: str = raw.get("phone", "") or ""
    data: dict[str, Any] = raw.get("data", {}) or {}
    created_at: str = raw.get("created_at", "") or ""
    updated_at: str = raw.get("updated_at", "") or ""

    content_parts: list[str] = [f"Name: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")
    if data:
        for k, v in data.items():
            if v:
                content_parts.append(f"{k}: {v}")

    source_id = _short_id(f"contact:{contact_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Contact: {name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "contact_id": contact_id,
            "email": email,
            "phone": phone,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_article(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Re:amaze knowledge base article into a ConnectorDocument.

    Stable source_id: sha256("article:" + slug)[:16]
    """
    slug: str = raw.get("slug", "") or str(raw.get("id", ""))
    title: str = raw.get("title", "") or f"Article {slug}"
    body: str = raw.get("body", "") or raw.get("body_text", "") or ""
    status: str = str(raw.get("status", "")) or "published"
    created_at: str = raw.get("created_at", "") or ""
    updated_at: str = raw.get("updated_at", "") or ""

    content_parts: list[str] = []
    if body:
        content_parts.append(body)
    content = "\n\n".join(content_parts) if content_parts else title

    source_id = _short_id(f"article:{slug}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "slug": slug,
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


# ── Retry helper ──────────────────────────────────────────────────────────────


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: ReamazeError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ReamazeAuthError:
            raise  # no retry on auth failures
        except ReamazeRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ReamazeError as exc:
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
