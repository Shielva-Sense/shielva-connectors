from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import DriftAuthError, DriftError, DriftRateLimitError
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
    Rate-limit errors honour the retryAfter value when present.
    """
    last_exc: DriftError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except DriftAuthError:
            raise
        except DriftRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except DriftError as exc:
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


def _short_hash(value: str) -> str:
    """Return a 16-character hex digest of SHA-256 for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def normalize_conversation(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Drift conversation into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of "conversation:{id}" so it
    remains deterministic and collision-resistant.
    """
    conv_id: int = raw.get("id", 0)
    status: str = raw.get("status", "")
    created_at: Any = raw.get("createdAt", raw.get("created_at"))
    updated_at: Any = raw.get("updatedAt", raw.get("updated_at"))
    subject: str = raw.get("subject", "") or ""
    contact_id: int = raw.get("contactId", raw.get("contact_id", 0)) or 0
    agent_id: int = raw.get("assignedAgentId", raw.get("agent_id", 0)) or 0

    display_subject = subject or f"Conversation {conv_id}"
    title = f"Conversation: {display_subject}"

    parts: list[str] = []
    if subject:
        parts.append(f"Subject: {subject}")
    if status:
        parts.append(f"Status: {status}")
    if contact_id:
        parts.append(f"Contact ID: {contact_id}")
    if agent_id:
        parts.append(f"Agent ID: {agent_id}")

    content = "\n".join(parts) if parts else display_subject
    source_id = _short_hash(f"conversation:{conv_id}")
    source_url = f"https://app.drift.com/conversations/{conv_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "conversation_id": conv_id,
            "status": status,
            "subject": subject,
            "contact_id": contact_id,
            "agent_id": agent_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_contact(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Drift contact into a ConnectorDocument."""
    contact_id: int = raw.get("id", 0)
    email: str = raw.get("email", "") or ""
    name: str = raw.get("name", "") or ""
    phone: str = raw.get("phone", "") or ""
    created_at: Any = raw.get("createdAt", raw.get("created_at"))
    updated_at: Any = raw.get("updatedAt", raw.get("updated_at"))
    attributes: dict[str, Any] = raw.get("attributes", {}) or {}
    company: str = attributes.get("company", "") or ""

    display_name = name or email or f"Contact {contact_id}"
    title = f"Contact: {display_name}"

    parts: list[str] = []
    if name:
        parts.append(f"Name: {name}")
    if email:
        parts.append(f"Email: {email}")
    if phone:
        parts.append(f"Phone: {phone}")
    if company:
        parts.append(f"Company: {company}")

    content = "\n".join(parts) if parts else display_name
    source_id = _short_hash(f"contact:{contact_id}")
    source_url = f"https://app.drift.com/contacts/{contact_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "contact_id": contact_id,
            "email": email,
            "name": name,
            "phone": phone,
            "company": company,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_account(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Drift account into a ConnectorDocument."""
    account_id: int = raw.get("id", raw.get("ownerId", 0))
    name: str = raw.get("name", "") or ""
    domain: str = raw.get("domain", "") or ""
    created_at: Any = raw.get("createdAt", raw.get("created_at"))

    display_name = name or domain or f"Account {account_id}"
    title = f"Account: {display_name}"

    parts: list[str] = []
    if name:
        parts.append(f"Name: {name}")
    if domain:
        parts.append(f"Domain: {domain}")

    content = "\n".join(parts) if parts else display_name
    source_id = _short_hash(f"account:{account_id}")
    source_url = f"https://app.drift.com/accounts/{account_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "account_id": account_id,
            "name": name,
            "domain": domain,
            "created_at": created_at,
        },
    )


def normalize_message(
    raw: dict[str, Any],
    conversation_id: int,
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Drift message into a ConnectorDocument."""
    message_id: int = raw.get("id", 0)
    body: str = raw.get("body", "") or ""
    author_id: int = raw.get("authorId", raw.get("author_id", 0)) or 0
    author_type: str = raw.get("type", "") or ""
    created_at: Any = raw.get("createdAt", raw.get("created_at"))

    display = body[:80] + ("..." if len(body) > 80 else "") if body else f"Message {message_id}"
    title = f"Message: {display}"

    parts: list[str] = []
    if author_type:
        parts.append(f"Type: {author_type}")
    if author_id:
        parts.append(f"Author ID: {author_id}")
    parts.append(f"Conversation ID: {conversation_id}")
    if body:
        parts.append(f"Body: {body}")

    content = "\n".join(parts) if parts else display
    source_id = _short_hash(f"message:{message_id}")
    source_url = f"https://app.drift.com/conversations/{conversation_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "message_id": message_id,
            "conversation_id": conversation_id,
            "body": body,
            "author_id": author_id,
            "author_type": author_type,
            "created_at": created_at,
        },
    )
