from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import FreshdeskAuthError, FreshdeskError, FreshdeskRateLimitError
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


def normalize_ticket(
    ticket: dict[str, Any],
    conversations: list[dict[str, Any]],
    connector_id: str,
    tenant_id: str,
    domain: str,
) -> ConnectorDocument:
    """Convert a raw Freshdesk ticket (+ its conversations) into a ConnectorDocument."""
    ticket_id: int = ticket.get("id", 0)
    subject: str = ticket.get("subject", "") or f"Ticket #{ticket_id}"
    description: str = ticket.get("description_text", "") or ticket.get("description", "") or ""
    status: Any = ticket.get("status", "")
    priority: Any = ticket.get("priority", "")
    ticket_type: str = ticket.get("type", "") or ""
    tags: list[str] = ticket.get("tags", []) or []
    requester_id: Any = ticket.get("requester_id", None)
    responder_id: Any = ticket.get("responder_id", None)
    created_at: str = ticket.get("created_at", "") or ""
    updated_at: str = ticket.get("updated_at", "") or ""

    # Build content from description + conversation bodies
    content_parts: list[str] = []
    if description:
        content_parts.append(f"Description:\n{description}")

    for conv in conversations:
        body: str = conv.get("body_text", "") or conv.get("body", "") or ""
        from_email: str = conv.get("from_email", "") or ""
        if body:
            header = f"Reply from {from_email}:" if from_email else "Reply:"
            content_parts.append(f"{header}\n{body}")

    content = "\n\n".join(content_parts) if content_parts else subject

    source_id = _short_id(str(ticket_id))

    return ConnectorDocument(
        source_id=source_id,
        title=f"Ticket #{ticket_id}: {subject}",
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://{domain}/helpdesk/tickets/{ticket_id}",
        metadata={
            "ticket_id": ticket_id,
            "status": status,
            "priority": priority,
            "type": ticket_type,
            "tags": tags,
            "requester_id": requester_id,
            "responder_id": responder_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_contact(
    contact: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    domain: str,
) -> ConnectorDocument:
    """Convert a raw Freshdesk contact into a ConnectorDocument."""
    contact_id: int = contact.get("id", 0)
    name: str = contact.get("name", "") or f"Contact #{contact_id}"
    email: str = contact.get("email", "") or ""
    phone: str = contact.get("phone", "") or contact.get("mobile", "") or ""
    company_id: Any = contact.get("company_id", None)
    created_at: str = contact.get("created_at", "") or ""
    job_title: str = contact.get("job_title", "") or ""
    twitter_id: str = contact.get("twitter_id", "") or ""

    content_parts: list[str] = [f"Name: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if company_id:
        content_parts.append(f"Company ID: {company_id}")
    if twitter_id:
        content_parts.append(f"Twitter: {twitter_id}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")

    source_id = _short_id(str(contact_id))

    return ConnectorDocument(
        source_id=source_id,
        title=f"Contact: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://{domain}/contacts/{contact_id}",
        metadata={
            "contact_id": contact_id,
            "email": email,
            "phone": phone,
            "company_id": company_id,
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
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: FreshdeskError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except FreshdeskAuthError:
            raise  # no retry on auth failures
        except FreshdeskRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except FreshdeskError as exc:
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
