"""Normalization helpers and retry logic for the Front connector."""
from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import FrontAuthError, FrontError, FrontRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

FRONT_APP_URL: str = "https://app.frontapp.com"


def _short_id(prefix: str, value: str) -> str:
    """Return a 16-character hex digest: sha256(prefix + value)[:16]."""
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_conversation(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Front conversation into a ConnectorDocument.

    The ``source_id`` is stable: sha256("conversation:" + id)[:16].
    """
    conv_id: str = str(raw.get("id", ""))
    subject: str = raw.get("subject", "") or f"Conversation {conv_id}"
    status: str = raw.get("status", "")
    created_at: str = str(raw.get("created_at", "") or "")
    last_message_raw: dict[str, Any] = raw.get("last_message") or {}
    updated_at: str = str(last_message_raw.get("created_at", "") or "")

    # Extract assignee info
    assignee: dict[str, Any] = raw.get("assignee", {}) or {}
    assignee_email: str = assignee.get("email", "") or ""
    assignee_name: str = assignee.get("first_name", "") or ""

    # Tags
    tags: list[str] = [t.get("name", "") for t in (raw.get("tags", []) or []) if t.get("name")]

    # Inbox name from _links or metadata
    inbox_name: str = ""
    for inbox in raw.get("inboxes", []) or []:
        inbox_name = inbox.get("name", "") or ""
        if inbox_name:
            break

    content_parts: list[str] = [f"Subject: {subject}"]
    if status:
        content_parts.append(f"Status: {status}")
    if assignee_name or assignee_email:
        content_parts.append(f"Assignee: {assignee_name} <{assignee_email}>".strip())
    if inbox_name:
        content_parts.append(f"Inbox: {inbox_name}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    if created_at:
        content_parts.append(f"Created: {created_at}")

    # Last message body (summary) — last_message_raw already guarded above
    last_msg: dict[str, Any] = last_message_raw
    last_body: str = last_msg.get("blurb", "") or last_msg.get("body", "") or ""
    if last_body:
        content_parts.append(f"Last message: {last_body[:500]}")

    source_id = _short_id("conversation", conv_id)
    permalink: str = (
        raw.get("_links", {}).get("related", {}).get("conversations", "")
        or f"{FRONT_APP_URL}/conversations/{conv_id}"
    )

    return ConnectorDocument(
        source_id=source_id,
        title=subject,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=permalink,
        metadata={
            "conversation_id": conv_id,
            "status": status,
            "assignee_email": assignee_email,
            "assignee_name": assignee_name,
            "tags": tags,
            "inbox": inbox_name,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_contact(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Front contact into a ConnectorDocument."""
    contact_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "") or f"Contact {contact_id}"
    description: str = raw.get("description", "") or ""
    avatar_url: str = raw.get("avatar_url", "") or ""

    # Collect handles (email, phone, etc.)
    handles: list[dict[str, Any]] = raw.get("handles", []) or []
    emails: list[str] = [
        h.get("handle", "") for h in handles if h.get("source") == "email" and h.get("handle")
    ]
    phones: list[str] = [
        h.get("handle", "") for h in handles if h.get("source") == "phone" and h.get("handle")
    ]

    groups: list[str] = [g.get("name", "") for g in (raw.get("groups", []) or []) if g.get("name")]
    is_spammer: bool = bool(raw.get("is_spammer", False))
    links: list[str] = raw.get("links", []) or []
    updated_at: str = str(raw.get("updated_at", "") or "")

    content_parts: list[str] = [f"Name: {name}"]
    if emails:
        content_parts.append(f"Email: {', '.join(emails)}")
    if phones:
        content_parts.append(f"Phone: {', '.join(phones)}")
    if description:
        content_parts.append(f"Description: {description}")
    if groups:
        content_parts.append(f"Groups: {', '.join(groups)}")
    if links:
        content_parts.append(f"Links: {', '.join(links)}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    source_id = _short_id("contact", contact_id)

    return ConnectorDocument(
        source_id=source_id,
        title=f"Contact: {name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=avatar_url,
        metadata={
            "contact_id": contact_id,
            "emails": emails,
            "phones": phones,
            "groups": groups,
            "is_spammer": is_spammer,
            "updated_at": updated_at,
        },
    )


def normalize_teammate(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Front teammate into a ConnectorDocument."""
    teammate_id: str = str(raw.get("id", ""))
    email: str = raw.get("email", "") or ""
    username: str = raw.get("username", "") or ""
    first_name: str = raw.get("first_name", "") or ""
    last_name: str = raw.get("last_name", "") or ""
    full_name: str = f"{first_name} {last_name}".strip() or username or email
    is_admin: bool = bool(raw.get("is_admin", False))
    is_available: bool = bool(raw.get("is_available", True))
    is_blocked: bool = bool(raw.get("is_blocked", False))

    content_parts: list[str] = [f"Name: {full_name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if username:
        content_parts.append(f"Username: {username}")
    content_parts.append(f"Admin: {'Yes' if is_admin else 'No'}")
    content_parts.append(f"Available: {'Yes' if is_available else 'No'}")
    if is_blocked:
        content_parts.append("Status: Blocked")

    source_id = _short_id("teammate", teammate_id)

    return ConnectorDocument(
        source_id=source_id,
        title=f"Teammate: {full_name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "teammate_id": teammate_id,
            "email": email,
            "username": username,
            "is_admin": is_admin,
            "is_available": is_available,
            "is_blocked": is_blocked,
        },
    )


def normalize_message(
    raw: dict[str, Any],
    conversation_id: str,
) -> ConnectorDocument:
    """Convert a raw Front message into a ConnectorDocument."""
    message_id: str = str(raw.get("id", ""))
    msg_type: str = raw.get("type", "") or ""
    is_inbound: bool = bool(raw.get("is_inbound", True))
    created_at: str = str(raw.get("created_at", "") or "")
    blurb: str = raw.get("blurb", "") or ""
    body: str = raw.get("body", "") or blurb or ""

    # Author info
    author: dict[str, Any] = raw.get("author", {}) or {}
    author_name: str = (
        f"{author.get('first_name', '')} {author.get('last_name', '')}".strip()
        or author.get("email", "")
        or "Unknown"
    )

    # Recipients
    recipients: list[dict[str, Any]] = raw.get("recipients", []) or []
    to_addrs: list[str] = [
        r.get("handle", "") for r in recipients
        if r.get("role") == "to" and r.get("handle")
    ]

    direction = "Inbound" if is_inbound else "Outbound"
    content_parts: list[str] = [
        f"Message ({direction}) in conversation {conversation_id}",
        f"From: {author_name}",
    ]
    if to_addrs:
        content_parts.append(f"To: {', '.join(to_addrs)}")
    if created_at:
        content_parts.append(f"Sent: {created_at}")
    if body:
        content_parts.append(f"\n{body[:2000]}")

    source_id = _short_id("message", message_id)

    return ConnectorDocument(
        source_id=source_id,
        title=f"Message from {author_name} ({direction})",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{FRONT_APP_URL}/conversations/{conversation_id}",
        metadata={
            "message_id": message_id,
            "conversation_id": conversation_id,
            "type": msg_type,
            "is_inbound": is_inbound,
            "author": author_name,
            "created_at": created_at,
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
    Rate-limit errors honour retry_after when it is > 0.
    """
    last_exc: FrontError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except FrontAuthError:
            raise  # never retry auth failures
        except FrontRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except FrontError as exc:
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
