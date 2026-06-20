from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import MarketoAuthError, MarketoError, MarketoRateLimitError
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

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: MarketoError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except MarketoAuthError:
            raise
        except MarketoRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except MarketoError as exc:
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


def _stable_id(prefix: str, raw_id: Any) -> str:
    """Return a stable 16-char hex id: sha256(prefix:raw_id)[:16]."""
    digest = hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()
    return digest[:16]


# ── Lead normalizer ───────────────────────────────────────────────────────────

def normalize_lead(raw: dict[str, Any], connector_id: str = "", tenant_id: str = "") -> ConnectorDocument:
    """Convert a raw Marketo lead dict into a ConnectorDocument.

    stable source_id = sha256("lead:" + str(id))[:16]
    """
    lead_id = raw.get("id", "")
    source_id = _stable_id("lead", lead_id)

    first = raw.get("firstName", "") or ""
    last = raw.get("lastName", "") or ""
    name = f"{first} {last}".strip() or f"Lead {lead_id}"
    email = raw.get("email", "") or ""
    company = raw.get("company", "") or ""
    title_field = raw.get("title", "") or ""
    phone = raw.get("phone", "") or ""
    created = raw.get("createdAt", "") or ""
    updated = raw.get("updatedAt", "") or ""

    doc_title = f"Marketo lead: {name}" + (f" <{email}>" if email else "")
    content_parts = [
        f"Lead ID: {lead_id}",
        f"Name: {name}",
        f"Email: {email}",
        f"Company: {company}",
        f"Title: {title_field}",
        f"Phone: {phone}",
        f"Created: {created}",
        f"Updated: {updated}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.marketo.com/#/lead/{lead_id}",
        metadata={
            "resource_type": "lead",
            "marketo_id": lead_id,
            "email": email,
            "name": name,
            "company": company,
            "phone": phone,
            "createdAt": created,
            "updatedAt": updated,
        },
    )


# ── List normalizer ───────────────────────────────────────────────────────────

def normalize_list(raw: dict[str, Any], connector_id: str = "", tenant_id: str = "") -> ConnectorDocument:
    """Convert a raw Marketo static list dict into a ConnectorDocument."""
    list_id = raw.get("id", "")
    source_id = _stable_id("list", list_id)

    list_name = raw.get("name", "") or f"List {list_id}"
    description = raw.get("description", "") or ""
    created = raw.get("createdAt", "") or ""
    updated = raw.get("updatedAt", "") or ""
    workspace = raw.get("workspaceName", "") or ""

    doc_title = f"Marketo list: {list_name}"
    content_parts = [
        f"List ID: {list_id}",
        f"Name: {list_name}",
        f"Description: {description}",
        f"Workspace: {workspace}",
        f"Created: {created}",
        f"Updated: {updated}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.marketo.com/#/staticLists/{list_id}",
        metadata={
            "resource_type": "list",
            "marketo_id": list_id,
            "name": list_name,
            "description": description,
            "workspaceName": workspace,
            "createdAt": created,
            "updatedAt": updated,
        },
    )


# ── Campaign normalizer ───────────────────────────────────────────────────────

def normalize_campaign(raw: dict[str, Any], connector_id: str = "", tenant_id: str = "") -> ConnectorDocument:
    """Convert a raw Marketo campaign dict into a ConnectorDocument."""
    campaign_id = raw.get("id", "")
    source_id = _stable_id("campaign", campaign_id)

    name = raw.get("name", "") or f"Campaign {campaign_id}"
    description = raw.get("description", "") or ""
    campaign_type = raw.get("type", "") or ""
    active = raw.get("active", False)
    created = raw.get("createdAt", "") or ""
    updated = raw.get("updatedAt", "") or ""
    workspace = raw.get("workspaceName", "") or ""

    doc_title = f"Marketo campaign: {name} ({campaign_type})"
    content_parts = [
        f"Campaign ID: {campaign_id}",
        f"Name: {name}",
        f"Type: {campaign_type}",
        f"Active: {active}",
        f"Description: {description}",
        f"Workspace: {workspace}",
        f"Created: {created}",
        f"Updated: {updated}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.marketo.com/#/campaigns/{campaign_id}",
        metadata={
            "resource_type": "campaign",
            "marketo_id": campaign_id,
            "name": name,
            "type": campaign_type,
            "active": active,
            "workspaceName": workspace,
            "createdAt": created,
            "updatedAt": updated,
        },
    )


# ── Program normalizer ────────────────────────────────────────────────────────

def normalize_program(raw: dict[str, Any], connector_id: str = "", tenant_id: str = "") -> ConnectorDocument:
    """Convert a raw Marketo program dict into a ConnectorDocument."""
    program_id = raw.get("id", "")
    source_id = _stable_id("program", program_id)

    name = raw.get("name", "") or f"Program {program_id}"
    description = raw.get("description", "") or ""
    program_type = raw.get("type", "") or ""
    channel = raw.get("channel", "") or ""
    status = raw.get("status", "") or ""
    created = raw.get("createdAt", "") or ""
    updated = raw.get("updatedAt", "") or ""
    workspace = raw.get("workspace", "") or ""

    doc_title = f"Marketo program: {name} ({program_type})"
    content_parts = [
        f"Program ID: {program_id}",
        f"Name: {name}",
        f"Type: {program_type}",
        f"Channel: {channel}",
        f"Status: {status}",
        f"Description: {description}",
        f"Workspace: {workspace}",
        f"Created: {created}",
        f"Updated: {updated}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.marketo.com/#/programs/{program_id}",
        metadata={
            "resource_type": "program",
            "marketo_id": program_id,
            "name": name,
            "type": program_type,
            "channel": channel,
            "status": status,
            "workspace": workspace,
            "createdAt": created,
            "updatedAt": updated,
        },
    )
