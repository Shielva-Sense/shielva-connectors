"""Shared utilities: normalization, retry logic with exponential backoff.

Imports only from stdlib and exceptions (bare module name — loaded by gateway
sys.path injection). No shared.* imports here to keep this module self-contained.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, Dict, Optional, TypeVar

from exceptions import (
    ConstantContactAuthError,
    ConstantContactError,
    ConstantContactRateLimitError,
)
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(prefix: str, resource_id: str) -> str:
    """Return a 16-char stable SHA-256 hex digest for a resource.

    Format: SHA-256("{prefix}:{resource_id}")[:16]
    Example: _stable_id("contact", "abc123") → deterministic 16-char hex string.
    """
    raw = f"{prefix}:{resource_id}"
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
    Network errors and generic connector errors are retried with backoff.
    """
    last_exc: Optional[ConstantContactError] = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except ConstantContactAuthError:
            raise
        except ConstantContactRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ConstantContactError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def normalize_contact(
    c: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a Constant Contact contact API response into a ConnectorDocument.

    ID = SHA-256("contact:{contact_id}")[:16]
    """
    contact_id: str = c.get("contact_id", "") or c.get("id", "")
    first_name: str = c.get("first_name", "") or ""
    last_name: str = c.get("last_name", "") or ""
    full_name: str = f"{first_name} {last_name}".strip() or "(unnamed)"

    email_addresses = c.get("email_address", {}) or {}
    if isinstance(email_addresses, dict):
        primary_email: str = email_addresses.get("address", "") or ""
    elif isinstance(email_addresses, list):
        primary_email = email_addresses[0].get("address", "") if email_addresses else ""
    else:
        primary_email = str(email_addresses)

    phone_numbers = c.get("phone_numbers", []) or []
    phone: str = ""
    if phone_numbers and isinstance(phone_numbers, list):
        phone = phone_numbers[0].get("phone_number", "") if phone_numbers else ""

    company: str = c.get("company_name", "") or ""
    job_title: str = c.get("job_title", "") or ""
    create_source: str = c.get("create_source", "") or ""
    created_at: str = c.get("created_at", "") or ""
    updated_at: str = c.get("updated_at", "") or ""

    list_memberships = c.get("list_memberships", []) or []

    content_parts = [f"Contact: {full_name}"]
    if primary_email:
        content_parts.append(f"Email: {primary_email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if company:
        content_parts.append(f"Company: {company}")
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")
    content: str = "\n".join(content_parts)

    metadata: Dict[str, Any] = {
        "contact_id": contact_id,
        "first_name": first_name,
        "last_name": last_name,
        "email": primary_email,
        "phone": phone,
        "company_name": company,
        "job_title": job_title,
        "create_source": create_source,
        "created_at": created_at,
        "updated_at": updated_at,
        "list_memberships": list_memberships,
    }

    return ConnectorDocument(
        id=_stable_id("contact", contact_id),
        source="constant_contact",
        type="contact",
        title=full_name,
        content=content,
        metadata=metadata,
        connector_id=connector_id,
        tenant_id=tenant_id,
    )


def normalize_campaign(
    c: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a Constant Contact email campaign into a ConnectorDocument.

    ID = SHA-256("campaign:{campaign_id}")[:16]
    """
    campaign_id: str = c.get("campaign_id", "") or c.get("id", "")
    name: str = c.get("name", "") or "(unnamed campaign)"
    status: str = c.get("current_status", "") or c.get("status", "") or ""
    campaign_type: str = c.get("campaign_type", "") or ""
    created_at: str = c.get("created_at", "") or ""
    updated_at: str = c.get("updated_at", "") or ""

    activity_ids = c.get("campaign_activities", []) or []

    content_parts = [f"Campaign: {name}"]
    if status:
        content_parts.append(f"Status: {status}")
    if campaign_type:
        content_parts.append(f"Type: {campaign_type}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")
    content: str = "\n".join(content_parts)

    metadata: Dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": name,
        "status": status,
        "campaign_type": campaign_type,
        "created_at": created_at,
        "updated_at": updated_at,
        "campaign_activities": activity_ids,
    }

    return ConnectorDocument(
        id=_stable_id("campaign", campaign_id),
        source="constant_contact",
        type="email_campaign",
        title=name,
        content=content,
        metadata=metadata,
        connector_id=connector_id,
        tenant_id=tenant_id,
    )


def normalize_list(
    l: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a Constant Contact contact list into a ConnectorDocument.

    ID = SHA-256("list:{list_id}")[:16]
    """
    list_id: str = l.get("list_id", "") or l.get("id", "")
    name: str = l.get("name", "") or "(unnamed list)"
    description: str = l.get("description", "") or ""
    status: str = l.get("status", "") or ""
    created_at: str = l.get("created_at", "") or ""
    updated_at: str = l.get("updated_at", "") or ""
    membership_count: int = l.get("membership_count", 0) or 0

    content_parts = [f"Contact List: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    if status:
        content_parts.append(f"Status: {status}")
    if membership_count:
        content_parts.append(f"Members: {membership_count}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")
    content: str = "\n".join(content_parts)

    metadata: Dict[str, Any] = {
        "list_id": list_id,
        "name": name,
        "description": description,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "membership_count": membership_count,
    }

    return ConnectorDocument(
        id=_stable_id("list", list_id),
        source="constant_contact",
        type="contact_list",
        title=name,
        content=content,
        metadata=metadata,
        connector_id=connector_id,
        tenant_id=tenant_id,
    )
