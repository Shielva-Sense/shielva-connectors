from __future__ import annotations

import asyncio
import hashlib
import os
import random
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

# Allow running from connector root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exceptions import GorgiasAuthError, GorgiasError, GorgiasRateLimitError
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

    Auth errors (GorgiasAuthError) are never retried — they require
    human intervention (credential rotation).
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: GorgiasError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except GorgiasAuthError:
            raise  # never retry auth failures
        except GorgiasRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except GorgiasError as exc:
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


def normalize_ticket(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    account: str = "",
) -> ConnectorDocument:
    """Convert a raw Gorgias ticket dict into a ConnectorDocument.

    source_id = sha256("ticket:" + str(id))[:16] — deterministic, 16-char.
    """
    ticket_id: int = raw.get("id", 0)
    subject: str = raw.get("subject", "") or f"Ticket #{ticket_id}"
    status: str = raw.get("status", "")
    channel: str = raw.get("channel", "")
    created_datetime: str = raw.get("created_datetime", "")
    updated_datetime: str = raw.get("updated_datetime", "")
    tags: list[str] = [t.get("name", "") for t in raw.get("tags", []) if isinstance(t, dict)]
    customer: dict[str, Any] = raw.get("customer", {}) or {}
    customer_id: int | None = customer.get("id") if customer else raw.get("customer_id")
    assignee: dict[str, Any] = raw.get("assignee_user", {}) or {}
    assignee_id: int | None = assignee.get("id") if assignee else raw.get("assignee_user_id")
    messages_count: int = raw.get("messages_count", 0)
    spam: bool = raw.get("spam", False)
    is_unread: bool = raw.get("is_unread", False)

    # Build content from last_message or subject
    last_message: dict[str, Any] = raw.get("last_message", {}) or {}
    body_text: str = last_message.get("body_text", "") or last_message.get("stripped_text", "") or subject
    content = f"Subject: {subject}\nStatus: {status}\nChannel: {channel}\n\n{body_text}"

    source_id = _short_hash(f"ticket:{ticket_id}")
    title = f"Ticket #{ticket_id}: {subject}"
    source_url = f"https://{account}.gorgias.com/app/ticket/{ticket_id}" if account else ""

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
            "channel": channel,
            "customer_id": customer_id,
            "assignee_user_id": assignee_id,
            "tags": tags,
            "messages_count": messages_count,
            "spam": spam,
            "is_unread": is_unread,
            "created_datetime": created_datetime,
            "updated_datetime": updated_datetime,
        },
    )


def normalize_customer(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    account: str = "",
) -> ConnectorDocument:
    """Convert a raw Gorgias customer dict into a ConnectorDocument.

    source_id = sha256("customer:" + str(id))[:16]
    """
    customer_id: int = raw.get("id", 0)
    email: str = raw.get("email", "")
    name: str = raw.get("name", "") or email or f"Customer #{customer_id}"
    external_id: str | None = raw.get("external_id")
    created_datetime: str = raw.get("created_datetime", "")
    updated_datetime: str = raw.get("updated_datetime", "")

    channels: list[dict[str, Any]] = raw.get("channels", []) or []
    channel_summary = ", ".join(
        f"{c.get('type', '')}:{c.get('address', '')}" for c in channels if c
    )

    content = f"Name: {name}\nEmail: {email}"
    if channel_summary:
        content += f"\nChannels: {channel_summary}"

    source_id = _short_hash(f"customer:{customer_id}")
    title = f"Customer: {name}"
    source_url = f"https://{account}.gorgias.com/app/customer/{customer_id}" if account else ""

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "customer_id": customer_id,
            "email": email,
            "name": name,
            "external_id": external_id,
            "channels": channels,
            "created_datetime": created_datetime,
            "updated_datetime": updated_datetime,
        },
    )


def normalize_macro(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    account: str = "",
) -> ConnectorDocument:
    """Convert a raw Gorgias macro dict into a ConnectorDocument.

    source_id = sha256("macro:" + str(id))[:16]
    """
    macro_id: int = raw.get("id", 0)
    name: str = raw.get("name", "") or f"Macro #{macro_id}"
    actions: list[dict[str, Any]] = raw.get("actions", []) or []
    created_datetime: str = raw.get("created_datetime", "")
    updated_datetime: str = raw.get("updated_datetime", "")

    action_summary = "; ".join(
        a.get("type", "") for a in actions if isinstance(a, dict)
    )
    content = f"Name: {name}"
    if action_summary:
        content += f"\nActions: {action_summary}"

    source_id = _short_hash(f"macro:{macro_id}")
    title = f"Macro: {name}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "macro_id": macro_id,
            "name": name,
            "actions": actions,
            "created_datetime": created_datetime,
            "updated_datetime": updated_datetime,
        },
    )


def normalize_tag(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    account: str = "",
) -> ConnectorDocument:
    """Convert a raw Gorgias tag dict into a ConnectorDocument.

    source_id = sha256("tag:" + str(id))[:16]
    """
    tag_id: int = raw.get("id", 0)
    name: str = raw.get("name", "") or f"Tag #{tag_id}"
    decoration: str | None = raw.get("decoration")

    content = f"Tag: {name}"
    if decoration:
        content += f"\nDecoration: {decoration}"

    source_id = _short_hash(f"tag:{tag_id}")
    title = f"Tag: {name}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "tag_id": tag_id,
            "name": name,
            "decoration": decoration,
        },
    )
