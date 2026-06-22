from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ZendeskSellAuthError, ZendeskSellError, ZendeskSellRateLimitError
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

    Auth errors (ZendeskSellAuthError) are never retried — they require
    human intervention (re-authorize the OAuth token).
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: ZendeskSellError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ZendeskSellAuthError:
            raise  # never retry auth failures
        except ZendeskSellRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ZendeskSellError as exc:
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


def stable_id(entity_type: str, entity_id: str | int) -> str:
    """Return a 16-character stable SHA-256 hex digest for a Zendesk Sell entity."""
    raw = f"{entity_type}:{entity_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Normalizers ──────────────────────────────────────────────────────────────

def normalize_contact(
    raw: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Zendesk Sell contact item into a ConnectorDocument.

    Zendesk Sell wraps each resource in ``{"data": {...}}``.  The caller may
    pass the inner ``data`` dict directly, or the outer wrapper — both work.
    """
    record: dict[str, Any] = raw.get("data", raw) or {}
    contact_id = str(record.get("id", ""))
    first = str(record.get("first_name", "") or "")
    last = str(record.get("last_name", "") or "")
    name = f"{first} {last}".strip() or f"Contact {contact_id}"
    email = str(record.get("email", "") or "")
    phone = str(record.get("phone", "") or "")
    mobile = str(record.get("mobile", "") or "")
    title_field = str(record.get("title", "") or "")
    organization_name = str(record.get("organization_name", "") or "")
    website = str(record.get("website", "") or "")
    description = str(record.get("description", "") or "")
    created_at = str(record.get("created_at", "") or "")
    updated_at = str(record.get("updated_at", "") or "")

    display_title = f"Zendesk Sell contact: {name}"
    if email:
        display_title += f" <{email}>"

    content_parts = [
        f"Contact ID: {contact_id}",
        f"Name: {name}",
        f"Email: {email}",
        f"Phone: {phone}",
        f"Mobile: {mobile}",
        f"Title: {title_field}",
        f"Organization: {organization_name}",
        f"Website: {website}",
        f"Description: {description}",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
    ]

    src_id = stable_id("contact", contact_id)
    return ConnectorDocument(
        source_id=src_id,
        title=display_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.futuresimple.com/crm/contacts/{contact_id}",
        metadata={
            "object_type": "contact",
            "contact_id": contact_id,
            "name": name,
            "email": email,
            "phone": phone,
            "mobile": mobile,
            "title": title_field,
            "organization_name": organization_name,
            "website": website,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_lead(
    raw: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Zendesk Sell lead item into a ConnectorDocument."""
    record: dict[str, Any] = raw.get("data", raw) or {}
    lead_id = str(record.get("id", ""))
    first = str(record.get("first_name", "") or "")
    last = str(record.get("last_name", "") or "")
    name = f"{first} {last}".strip() or f"Lead {lead_id}"
    email = str(record.get("email", "") or "")
    phone = str(record.get("phone", "") or "")
    organization_name = str(record.get("organization_name", "") or "")
    title_field = str(record.get("title", "") or "")
    status = str(record.get("status", "") or "")
    source_id_raw = str(record.get("source_id", "") or "")
    description = str(record.get("description", "") or "")
    created_at = str(record.get("created_at", "") or "")
    updated_at = str(record.get("updated_at", "") or "")

    content_parts = [
        f"Lead ID: {lead_id}",
        f"Name: {name}",
        f"Email: {email}",
        f"Phone: {phone}",
        f"Organization: {organization_name}",
        f"Title: {title_field}",
        f"Status: {status}",
        f"Source ID: {source_id_raw}",
        f"Description: {description}",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
    ]

    src_id = stable_id("lead", lead_id)
    return ConnectorDocument(
        source_id=src_id,
        title=f"Zendesk Sell lead: {name}",
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.futuresimple.com/crm/leads/{lead_id}",
        metadata={
            "object_type": "lead",
            "lead_id": lead_id,
            "name": name,
            "email": email,
            "phone": phone,
            "organization_name": organization_name,
            "title": title_field,
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_deal(
    raw: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Zendesk Sell deal item into a ConnectorDocument."""
    record: dict[str, Any] = raw.get("data", raw) or {}
    deal_id = str(record.get("id", ""))
    name = str(record.get("name", "") or f"Deal {deal_id}")
    value = str(record.get("value", "") or "")
    currency = str(record.get("currency", "") or "")
    status = str(record.get("status", "") or "")
    stage_id = str(record.get("stage_id", "") or "")
    owner_id = str(record.get("owner_id", "") or "")
    contact_id = str(record.get("contact_id", "") or "")
    organization_id = str(record.get("organization_id", "") or "")
    expected_close_date = str(record.get("expected_close_date", "") or "")
    created_at = str(record.get("created_at", "") or "")
    updated_at = str(record.get("updated_at", "") or "")

    value_display = f"{value} {currency}".strip() if value else "N/A"
    content_parts = [
        f"Deal ID: {deal_id}",
        f"Name: {name}",
        f"Value: {value_display}",
        f"Status: {status}",
        f"Stage ID: {stage_id}",
        f"Owner ID: {owner_id}",
        f"Contact ID: {contact_id}",
        f"Organization ID: {organization_id}",
        f"Expected close: {expected_close_date}",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
    ]

    src_id = stable_id("deal", deal_id)
    return ConnectorDocument(
        source_id=src_id,
        title=f"Zendesk Sell deal: {name}",
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.futuresimple.com/sales/deals/{deal_id}",
        metadata={
            "object_type": "deal",
            "deal_id": deal_id,
            "name": name,
            "value": value,
            "currency": currency,
            "status": status,
            "stage_id": stage_id,
            "owner_id": owner_id,
            "contact_id": contact_id,
            "organization_id": organization_id,
            "expected_close_date": expected_close_date,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_note(
    raw: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Zendesk Sell note item into a ConnectorDocument."""
    record: dict[str, Any] = raw.get("data", raw) or {}
    note_id = str(record.get("id", ""))
    content_text = str(record.get("content", "") or "")
    resource_type = str(record.get("resource_type", "") or "")
    resource_id = str(record.get("resource_id", "") or "")
    creator_id = str(record.get("creator_id", "") or "")
    created_at = str(record.get("created_at", "") or "")
    updated_at = str(record.get("updated_at", "") or "")

    content_parts = [
        f"Note ID: {note_id}",
        f"Content: {content_text}",
        f"Resource type: {resource_type}",
        f"Resource ID: {resource_id}",
        f"Creator ID: {creator_id}",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
    ]

    src_id = stable_id("note", note_id)
    title_preview = content_text[:60] + "…" if len(content_text) > 60 else content_text
    return ConnectorDocument(
        source_id=src_id,
        title=f"Zendesk Sell note: {title_preview or note_id}",
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "note",
            "note_id": note_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "creator_id": creator_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_task(
    raw: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Zendesk Sell task item into a ConnectorDocument."""
    record: dict[str, Any] = raw.get("data", raw) or {}
    task_id = str(record.get("id", ""))
    content_text = str(record.get("content", "") or "")
    due_date = str(record.get("due_date", "") or "")
    status = str(record.get("status", "") or "")
    resource_type = str(record.get("resource_type", "") or "")
    resource_id = str(record.get("resource_id", "") or "")
    owner_id = str(record.get("owner_id", "") or "")
    completed = str(record.get("completed", "") or "")
    created_at = str(record.get("created_at", "") or "")
    updated_at = str(record.get("updated_at", "") or "")

    content_parts = [
        f"Task ID: {task_id}",
        f"Content: {content_text}",
        f"Due date: {due_date}",
        f"Status: {status}",
        f"Completed: {completed}",
        f"Resource type: {resource_type}",
        f"Resource ID: {resource_id}",
        f"Owner ID: {owner_id}",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
    ]

    src_id = stable_id("task", task_id)
    title_preview = content_text[:60] + "…" if len(content_text) > 60 else content_text
    return ConnectorDocument(
        source_id=src_id,
        title=f"Zendesk Sell task: {title_preview or task_id}",
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "task",
            "task_id": task_id,
            "due_date": due_date,
            "status": status,
            "completed": completed,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "owner_id": owner_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
