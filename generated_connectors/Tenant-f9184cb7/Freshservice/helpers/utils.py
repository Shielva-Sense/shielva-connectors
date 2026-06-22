from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import FreshserviceAuthError, FreshserviceError, FreshserviceRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(prefix: str, value: str) -> str:
    """Return a 16-character hex digest (sha256 prefix) for the given prefixed value."""
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:16]


# ── Ticket normalizer ─────────────────────────────────────────────────────────


def normalize_ticket(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    subdomain: str = "",
) -> ConnectorDocument:
    """Convert a raw Freshservice ITSM ticket into a ConnectorDocument."""
    ticket_id: int = raw.get("id", 0)
    subject: str = raw.get("subject", "") or f"Ticket #{ticket_id}"
    description: str = (
        raw.get("description_text", "")
        or raw.get("description", "")
        or ""
    )
    status: Any = raw.get("status", "")
    priority: Any = raw.get("priority", "")
    ticket_type: str = raw.get("type", "") or raw.get("ticket_type", "") or ""
    category: str = raw.get("category", "") or ""
    sub_category: str = raw.get("sub_category", "") or ""
    tags: list[str] = raw.get("tags", []) or []
    requester_id: Any = raw.get("requester_id", None)
    responder_id: Any = raw.get("responder_id", None)
    group_id: Any = raw.get("group_id", None)
    department_id: Any = raw.get("department_id", None)
    created_at: str = raw.get("created_at", "") or ""
    updated_at: str = raw.get("updated_at", "") or ""

    content_parts: list[str] = []
    if subject:
        content_parts.append(f"Subject: {subject}")
    if description:
        content_parts.append(f"Description:\n{description}")
    if category:
        content_parts.append(f"Category: {category}")
    if sub_category:
        content_parts.append(f"Sub-category: {sub_category}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")

    content = "\n\n".join(content_parts) if content_parts else subject

    source_id = _short_id("ticket", str(ticket_id))

    return ConnectorDocument(
        source_id=source_id,
        title=f"Ticket #{ticket_id}: {subject}",
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=(
            f"https://{subdomain}.freshservice.com/helpdesk/tickets/{ticket_id}"
            if subdomain
            else ""
        ),
        metadata={
            "ticket_id": ticket_id,
            "status": status,
            "priority": priority,
            "type": ticket_type,
            "category": category,
            "sub_category": sub_category,
            "tags": tags,
            "requester_id": requester_id,
            "responder_id": responder_id,
            "group_id": group_id,
            "department_id": department_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


# ── Asset normalizer ──────────────────────────────────────────────────────────


def normalize_asset(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    subdomain: str = "",
) -> ConnectorDocument:
    """Convert a raw Freshservice CMDB asset (CI) into a ConnectorDocument."""
    asset_id: int = raw.get("id", 0)
    name: str = raw.get("name", "") or f"Asset #{asset_id}"
    asset_type: str = raw.get("asset_type_name", "") or raw.get("asset_type", "") or ""
    description: str = raw.get("description", "") or ""
    serial_number: str = raw.get("serial_number", "") or ""
    asset_tag: str = raw.get("asset_tag", "") or ""
    location: str = raw.get("location_name", "") or raw.get("location", "") or ""
    department: str = raw.get("department_name", "") or raw.get("department", "") or ""
    user: str = raw.get("user_name", "") or raw.get("user", "") or ""
    state: str = raw.get("state", "") or ""
    created_at: str = raw.get("created_at", "") or ""
    updated_at: str = raw.get("updated_at", "") or ""

    content_parts: list[str] = [f"Name: {name}"]
    if asset_type:
        content_parts.append(f"Type: {asset_type}")
    if description:
        content_parts.append(f"Description: {description}")
    if serial_number:
        content_parts.append(f"Serial Number: {serial_number}")
    if asset_tag:
        content_parts.append(f"Asset Tag: {asset_tag}")
    if location:
        content_parts.append(f"Location: {location}")
    if department:
        content_parts.append(f"Department: {department}")
    if user:
        content_parts.append(f"Assigned User: {user}")
    if state:
        content_parts.append(f"State: {state}")

    source_id = _short_id("asset", str(asset_id))

    return ConnectorDocument(
        source_id=source_id,
        title=f"Asset: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=(
            f"https://{subdomain}.freshservice.com/cmdb/items/{asset_id}"
            if subdomain
            else ""
        ),
        metadata={
            "asset_id": asset_id,
            "asset_type": asset_type,
            "serial_number": serial_number,
            "asset_tag": asset_tag,
            "location": location,
            "department": department,
            "state": state,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


# ── Agent normalizer ──────────────────────────────────────────────────────────


def normalize_agent(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    subdomain: str = "",
) -> ConnectorDocument:
    """Convert a raw Freshservice agent into a ConnectorDocument."""
    agent_id: int = raw.get("id", 0)
    # Freshservice may nest contact info
    contact: dict[str, Any] = raw.get("contact", {}) or {}
    name: str = (
        contact.get("name", "")
        or raw.get("name", "")
        or f"Agent #{agent_id}"
    )
    email: str = contact.get("email", "") or raw.get("email", "") or ""
    phone: str = contact.get("phone", "") or raw.get("phone", "") or ""
    job_title: str = raw.get("job_title", "") or contact.get("job_title", "") or ""
    department: str = (
        raw.get("department_name", "")
        or raw.get("department", "")
        or ""
    )
    available: bool = raw.get("available", False)
    created_at: str = raw.get("created_at", "") or ""
    updated_at: str = raw.get("updated_at", "") or ""

    content_parts: list[str] = [f"Name: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if department:
        content_parts.append(f"Department: {department}")
    content_parts.append(f"Available: {available}")

    source_id = _short_id("agent", str(agent_id))

    return ConnectorDocument(
        source_id=source_id,
        title=f"Agent: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "agent_id": agent_id,
            "email": email,
            "phone": phone,
            "job_title": job_title,
            "department": department,
            "available": available,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


# ── Change normalizer ─────────────────────────────────────────────────────────


def normalize_change(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    subdomain: str = "",
) -> ConnectorDocument:
    """Convert a raw Freshservice change request into a ConnectorDocument."""
    change_id: int = raw.get("id", 0)
    subject: str = raw.get("subject", "") or f"Change #{change_id}"
    description: str = (
        raw.get("description_text", "")
        or raw.get("description", "")
        or ""
    )
    status: Any = raw.get("status", "")
    priority: Any = raw.get("priority", "")
    change_type: str = raw.get("change_type", "") or ""
    risk: str = raw.get("risk", "") or ""
    category: str = raw.get("category", "") or ""
    planned_start_date: str = raw.get("planned_start_date", "") or ""
    planned_end_date: str = raw.get("planned_end_date", "") or ""
    requester_id: Any = raw.get("requester_id", None)
    agent_id: Any = raw.get("agent_id", None)
    group_id: Any = raw.get("group_id", None)
    created_at: str = raw.get("created_at", "") or ""
    updated_at: str = raw.get("updated_at", "") or ""

    content_parts: list[str] = [f"Subject: {subject}"]
    if description:
        content_parts.append(f"Description:\n{description}")
    if change_type:
        content_parts.append(f"Change Type: {change_type}")
    if risk:
        content_parts.append(f"Risk: {risk}")
    if category:
        content_parts.append(f"Category: {category}")
    if planned_start_date:
        content_parts.append(f"Planned Start: {planned_start_date}")
    if planned_end_date:
        content_parts.append(f"Planned End: {planned_end_date}")

    content = "\n\n".join(content_parts) if content_parts else subject

    source_id = _short_id("change", str(change_id))

    return ConnectorDocument(
        source_id=source_id,
        title=f"Change #{change_id}: {subject}",
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=(
            f"https://{subdomain}.freshservice.com/changes/{change_id}"
            if subdomain
            else ""
        ),
        metadata={
            "change_id": change_id,
            "status": status,
            "priority": priority,
            "change_type": change_type,
            "risk": risk,
            "category": category,
            "planned_start_date": planned_start_date,
            "planned_end_date": planned_end_date,
            "requester_id": requester_id,
            "agent_id": agent_id,
            "group_id": group_id,
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
    last_exc: FreshserviceError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except FreshserviceAuthError:
            raise  # no retry on auth failures
        except FreshserviceRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except FreshserviceError as exc:
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
