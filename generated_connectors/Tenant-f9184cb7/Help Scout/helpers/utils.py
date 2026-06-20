from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import HelpScoutAuthError, HelpScoutError, HelpScoutRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

HELPSCOUT_APP_URL: str = "https://secure.helpscout.net"


def _short_id(value: str) -> str:
    """Return a 16-character hex digest (sha256 prefix) for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_conversation(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Help Scout conversation into a ConnectorDocument.

    The stable source_id is sha256("conversation:" + str(id))[:16].
    """
    conv_id: int = raw.get("id", 0)
    subject: str = raw.get("subject", "") or f"Conversation #{conv_id}"
    status: str = raw.get("status", "") or ""
    conv_type: str = raw.get("type", "") or ""
    preview: str = raw.get("preview", "") or ""

    # Customer reference
    customer_raw: dict[str, Any] = raw.get("createdBy", {}) or {}
    customer_name: str = (
        f"{customer_raw.get('first', '')} {customer_raw.get('last', '')}".strip()
        or customer_raw.get("email", "")
        or "Unknown"
    )

    # Assignee reference
    assignee_raw: dict[str, Any] = raw.get("assignee", {}) or {}
    assignee_name: str = (
        f"{assignee_raw.get('first', '')} {assignee_raw.get('last', '')}".strip()
        or assignee_raw.get("email", "")
        or ""
    )

    # Mailbox reference
    mailbox_raw: dict[str, Any] = raw.get("mailboxRef", {}) or {}
    mailbox_name: str = mailbox_raw.get("name", "") or ""

    # Tags
    tags: list[str] = [t.get("tag", "") for t in (raw.get("tags") or []) if t.get("tag")]

    created_at: str = raw.get("createdAt", "") or ""
    updated_at: str = raw.get("userUpdatedAt", "") or raw.get("updatedAt", "") or ""
    thread_count: int = raw.get("threads", 0) or 0

    content_parts: list[str] = [f"Subject: {subject}"]
    if preview:
        content_parts.append(f"Preview: {preview}")
    if customer_name and customer_name != "Unknown":
        content_parts.append(f"Customer: {customer_name}")
    if assignee_name:
        content_parts.append(f"Assignee: {assignee_name}")
    if mailbox_name:
        content_parts.append(f"Mailbox: {mailbox_name}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    if status:
        content_parts.append(f"Status: {status}")
    if conv_type:
        content_parts.append(f"Type: {conv_type}")

    source_id = _short_id(f"conversation:{conv_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Conversation #{conv_id}: {subject}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{HELPSCOUT_APP_URL}/conversations/{conv_id}",
        metadata={
            "conversation_id": conv_id,
            "status": status,
            "type": conv_type,
            "customer_name": customer_name,
            "assignee_name": assignee_name,
            "mailbox_name": mailbox_name,
            "tags": tags,
            "thread_count": thread_count,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_customer(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Help Scout customer into a ConnectorDocument.

    The stable source_id is sha256("customer:" + str(id))[:16].
    """
    cust_id: int = raw.get("id", 0)
    first: str = raw.get("firstName", "") or ""
    last: str = raw.get("lastName", "") or ""
    name: str = f"{first} {last}".strip() or f"Customer #{cust_id}"

    # Email — nested under _embedded.emails or top-level email
    emails_raw: list[dict[str, Any]] = []
    embedded: dict[str, Any] = raw.get("_embedded", {}) or {}
    if "emails" in embedded:
        emails_raw = embedded["emails"] or []
    email: str = ""
    if emails_raw:
        email = emails_raw[0].get("value", "") or ""
    if not email:
        email = raw.get("email", "") or ""

    # Phone
    phones_raw: list[dict[str, Any]] = embedded.get("phones", []) or []
    phone: str = ""
    if phones_raw:
        phone = phones_raw[0].get("value", "") or ""

    company: str = raw.get("company", "") or ""
    job_title: str = raw.get("jobTitle", "") or ""
    created_at: str = raw.get("createdAt", "") or ""
    updated_at: str = raw.get("updatedAt", "") or ""
    background: str = raw.get("background", "") or ""

    content_parts: list[str] = [f"Name: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if company:
        content_parts.append(f"Company: {company}")
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if background:
        content_parts.append(f"Background: {background}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")

    source_id = _short_id(f"customer:{cust_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Customer: {name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{HELPSCOUT_APP_URL}/customers/{cust_id}",
        metadata={
            "customer_id": cust_id,
            "email": email,
            "phone": phone,
            "company": company,
            "job_title": job_title,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_mailbox(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Help Scout mailbox into a ConnectorDocument.

    The stable source_id is sha256("mailbox:" + str(id))[:16].
    """
    mb_id: int = raw.get("id", 0)
    name: str = raw.get("name", "") or f"Mailbox #{mb_id}"
    slug: str = raw.get("slug", "") or ""
    email: str = raw.get("email", "") or ""
    created_at: str = raw.get("createdAt", "") or ""
    updated_at: str = raw.get("updatedAt", "") or ""

    content_parts: list[str] = [f"Mailbox: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if slug:
        content_parts.append(f"Slug: {slug}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")

    source_id = _short_id(f"mailbox:{mb_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Mailbox: {name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{HELPSCOUT_APP_URL}/mailboxes/{mb_id}",
        metadata={
            "mailbox_id": mb_id,
            "name": name,
            "slug": slug,
            "email": email,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_user(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Help Scout user into a ConnectorDocument.

    The stable source_id is sha256("user:" + str(id))[:16].
    """
    user_id: int = raw.get("id", 0)
    first: str = raw.get("firstName", "") or ""
    last: str = raw.get("lastName", "") or ""
    name: str = f"{first} {last}".strip() or f"User #{user_id}"
    email: str = raw.get("email", "") or ""
    role: str = raw.get("role", "") or ""
    timezone: str = raw.get("timezone", "") or ""
    created_at: str = raw.get("createdAt", "") or ""
    updated_at: str = raw.get("updatedAt", "") or ""

    content_parts: list[str] = [f"Name: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if role:
        content_parts.append(f"Role: {role}")
    if timezone:
        content_parts.append(f"Timezone: {timezone}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")

    source_id = _short_id(f"user:{user_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"User: {name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{HELPSCOUT_APP_URL}/users/{user_id}",
        metadata={
            "user_id": user_id,
            "email": email,
            "role": role,
            "timezone": timezone,
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
    last_exc: HelpScoutError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except HelpScoutAuthError:
            raise  # no retry on auth failures
        except HelpScoutRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except HelpScoutError as exc:
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
