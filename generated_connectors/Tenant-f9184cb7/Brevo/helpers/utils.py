from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import (
    BrevoAuthError,
    BrevoError,
    BrevoRateLimitError,
)
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(raw: str) -> str:
    """Return a 16-char stable SHA-256 hex digest for a raw string."""
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
    last_exc: BrevoError | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except BrevoAuthError:
            raise
        except BrevoRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except BrevoError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_contact(
    c: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Brevo contact into a ConnectorDocument.

    source_id = SHA-256("contact:{id}:{email}")[:16]
    """
    contact_id = str(c.get("id", ""))
    email = c.get("email", "") or ""
    raw = f"contact:{contact_id}:{email}"
    source_id = _stable_id(raw)

    attributes = c.get("attributes", {}) or {}
    first = attributes.get("FIRSTNAME", "") or ""
    last = attributes.get("LASTNAME", "") or ""
    name = f"{first} {last}".strip() or "Unknown"
    list_ids = c.get("listIds", []) or []
    created = c.get("createdAt", "") or ""
    updated = c.get("modifiedAt", "") or ""

    title = f"Brevo contact: {name}" + (f" <{email}>" if email else "")
    content_parts = [
        f"Contact ID: {contact_id}",
        f"Name: {name}",
        f"Email: {email}",
        f"Lists: {', '.join(str(lid) for lid in list_ids)}",
        f"Created: {created}",
        f"Last updated: {updated}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "contact",
            "brevo_id": contact_id,
            "email": email,
            "name": name,
            "list_ids": list_ids,
            "createdAt": created,
            "modifiedAt": updated,
        },
    )


def normalize_campaign(
    c: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Brevo email campaign into a ConnectorDocument.

    source_id = SHA-256("campaign:{id}")[:16]
    """
    campaign_id = str(c.get("id", ""))
    raw = f"campaign:{campaign_id}"
    source_id = _stable_id(raw)

    name = c.get("name", "") or f"Campaign {campaign_id}"
    subject = c.get("subject", "") or ""
    status = c.get("status", "") or ""
    sent_date = c.get("sentDate", "") or ""
    created = c.get("createdAt", "") or ""
    stats = c.get("statistics", {}) or {}

    title = f"Brevo campaign: {name} ({status})"
    content_parts = [
        f"Campaign ID: {campaign_id}",
        f"Name: {name}",
        f"Subject: {subject}",
        f"Status: {status}",
        f"Sent date: {sent_date}",
        f"Created: {created}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "email_campaign",
            "brevo_id": campaign_id,
            "name": name,
            "subject": subject,
            "status": status,
            "sentDate": sent_date,
            "createdAt": created,
            "statistics": stats,
        },
    )


def normalize_template(
    t: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Brevo SMTP template into a ConnectorDocument.

    source_id = SHA-256("template:{id}")[:16]
    """
    template_id = str(t.get("id", ""))
    raw = f"template:{template_id}"
    source_id = _stable_id(raw)

    name = t.get("name", "") or f"Template {template_id}"
    subject = t.get("subject", "") or ""
    tag = t.get("tag", "") or ""
    is_active = t.get("isActive", False)
    created = t.get("createdAt", "") or ""
    modified = t.get("modifiedAt", "") or ""

    title = f"Brevo template: {name}"
    content_parts = [
        f"Template ID: {template_id}",
        f"Name: {name}",
        f"Subject: {subject}",
        f"Tag: {tag}",
        f"Active: {is_active}",
        f"Created: {created}",
        f"Modified: {modified}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "email_template",
            "brevo_id": template_id,
            "name": name,
            "subject": subject,
            "tag": tag,
            "isActive": is_active,
            "createdAt": created,
            "modifiedAt": modified,
        },
    )
