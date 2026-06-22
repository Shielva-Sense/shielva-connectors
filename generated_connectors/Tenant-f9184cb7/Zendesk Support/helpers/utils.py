from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ZendeskAuthError, ZendeskError, ZendeskRateLimitError
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

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: ZendeskError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ZendeskAuthError:
            raise
        except ZendeskRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ZendeskError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _short_hash(value: str) -> str:
    """Return a 16-character hex digest of SHA-256 for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def normalize_ticket(
    ticket: dict[str, Any],
    comments: list[dict[str, Any]],
    connector_id: str,
    tenant_id: str,
    subdomain: str,
) -> ConnectorDocument:
    """Convert a raw Zendesk ticket + its comments into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of the ticket ID string so it
    fits within Shielva's canonical 16-char source_id budget while remaining
    deterministic and collision-resistant for any realistic ticket volume.
    """
    ticket_id: int = ticket.get("id", 0)
    subject: str = ticket.get("subject", "") or f"Ticket #{ticket_id}"
    description: str = ticket.get("description", "") or ""
    status: str = ticket.get("status", "")
    priority: str | None = ticket.get("priority")
    requester_id: int | None = ticket.get("requester_id")
    assignee_id: int | None = ticket.get("assignee_id")
    tags: list[str] = ticket.get("tags", [])
    created_at: str = ticket.get("created_at", "")
    updated_at: str = ticket.get("updated_at", "")

    # Build content: description + each comment body
    content_parts: list[str] = [description]
    for comment in comments:
        body: str = comment.get("body", "") or comment.get("plain_body", "") or ""
        if body:
            author_id: Any = comment.get("author_id", "")
            content_parts.append(f"[Comment by user {author_id}]: {body}")

    content = "\n\n".join(part for part in content_parts if part)

    source_id = _short_hash(str(ticket_id))
    title = f"Ticket #{ticket_id}: {subject}"
    source_url = f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "ticket_id": ticket_id,
            "status": status,
            "priority": priority,
            "requester_id": requester_id,
            "assignee_id": assignee_id,
            "tags": tags,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_user(
    user: dict[str, Any],
    subdomain: str,
) -> ConnectorDocument:
    """Convert a raw Zendesk user into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of ``"user:" + str(user["id"])``,
    matching the spec's ``sha256("user:"+str(user["id"]))[:16]`` formula.
    """
    user_id: int = user.get("id", 0)
    name: str = user.get("name", "") or f"User #{user_id}"
    email: str = user.get("email", "") or ""
    role: str = user.get("role", "") or ""
    phone: str = user.get("phone", "") or ""
    time_zone: str = user.get("time_zone", "") or ""
    locale: str = user.get("locale", "") or ""
    created_at: str = user.get("created_at", "") or ""
    updated_at: str = user.get("updated_at", "") or ""
    organization_id: int | None = user.get("organization_id")
    active: bool = bool(user.get("active", True))

    content_parts: list[str] = [f"Name: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if role:
        content_parts.append(f"Role: {role}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if organization_id:
        content_parts.append(f"Organization ID: {organization_id}")
    if time_zone:
        content_parts.append(f"Time Zone: {time_zone}")
    if locale:
        content_parts.append(f"Locale: {locale}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")

    source_id = _short_hash(f"user:{user_id}")
    source_url = f"https://{subdomain}.zendesk.com/agent/users/{user_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=f"User: {name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "user_id": user_id,
            "email": email,
            "role": role,
            "phone": phone,
            "organization_id": organization_id,
            "active": active,
            "time_zone": time_zone,
            "locale": locale,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
