from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import IntercomAuthError, IntercomError, IntercomRateLimitError
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
    Rate-limit errors honour the X-RateLimit-Reset header when present.
    """
    last_exc: IntercomError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except IntercomAuthError:
            raise
        except IntercomRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except IntercomError as exc:
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


def normalize_contact(
    contact: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw Intercom contact into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of the contact ID string so it
    fits within Shielva's canonical 16-char source_id budget while remaining
    deterministic and collision-resistant.
    """
    contact_id: str = str(contact.get("id", ""))
    role: str = contact.get("role", "")  # "user" or "lead"
    name: str = contact.get("name", "") or ""
    email: str = contact.get("email", "") or ""
    phone: str = contact.get("phone", "") or ""
    created_at: Any = contact.get("created_at")
    updated_at: Any = contact.get("updated_at")
    external_id: str = contact.get("external_id", "") or ""
    location: dict[str, Any] = contact.get("location", {}) or {}
    company_name: str = ""
    companies: dict[str, Any] = contact.get("companies", {}) or {}
    company_list: list[dict[str, Any]] = companies.get("data", [])
    if company_list:
        company_name = company_list[0].get("id", "")

    display_name = name or email or f"Contact {contact_id}"
    title = f"Contact: {display_name}"

    parts: list[str] = []
    if name:
        parts.append(f"Name: {name}")
    if email:
        parts.append(f"Email: {email}")
    if phone:
        parts.append(f"Phone: {phone}")
    if role:
        parts.append(f"Role: {role}")
    if external_id:
        parts.append(f"External ID: {external_id}")
    if location.get("country"):
        parts.append(f"Country: {location['country']}")
    if location.get("city"):
        parts.append(f"City: {location['city']}")

    content = "\n".join(parts) if parts else display_name

    source_id = _short_hash(f"contact:{contact_id}")
    source_url = f"https://app.intercom.com/contacts/{contact_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "contact_id": contact_id,
            "role": role,
            "name": name,
            "email": email,
            "phone": phone,
            "external_id": external_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "company_name": company_name,
        },
    )


def normalize_conversation(
    conversation: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw Intercom conversation into a ConnectorDocument."""
    conv_id: str = str(conversation.get("id", ""))
    state: str = conversation.get("state", "")
    read: bool = conversation.get("read", False)
    created_at: Any = conversation.get("created_at")
    updated_at: Any = conversation.get("updated_at")

    # First conversation part (the opener)
    source: dict[str, Any] = conversation.get("source", {}) or {}
    subject: str = source.get("subject", "") or ""
    body: str = source.get("body", "") or ""

    # Conversation parts (replies)
    conv_parts: dict[str, Any] = conversation.get("conversation_parts", {}) or {}
    parts_list: list[dict[str, Any]] = conv_parts.get("conversation_parts", [])

    # Assignee
    assignee: dict[str, Any] = conversation.get("assignee", {}) or {}
    assignee_name: str = assignee.get("name", "") or ""

    # Contact (initiator)
    contacts: dict[str, Any] = conversation.get("contacts", {}) or {}
    contact_list: list[dict[str, Any]] = contacts.get("contacts", [])
    contact_email: str = ""
    if contact_list:
        contact_email = contact_list[0].get("email", "") or ""

    display_subject = subject or f"Conversation {conv_id}"
    title = f"Conversation: {display_subject}"

    content_parts: list[str] = []
    if body:
        content_parts.append(f"[Opening message]: {body}")
    for part in parts_list:
        part_body: str = part.get("body", "") or ""
        if part_body:
            author: dict[str, Any] = part.get("author", {}) or {}
            author_name: str = author.get("name", "") or "Unknown"
            content_parts.append(f"[{author_name}]: {part_body}")

    content = "\n\n".join(content_parts) if content_parts else display_subject

    source_id = _short_hash(f"conversation:{conv_id}")
    source_url = f"https://app.intercom.com/conversations/{conv_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "conversation_id": conv_id,
            "state": state,
            "read": read,
            "assignee_name": assignee_name,
            "contact_email": contact_email,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
