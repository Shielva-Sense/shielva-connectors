from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ServiceNowAuthError, ServiceNowError, ServiceNowRateLimitError
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
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: ServiceNowError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ServiceNowAuthError:
            raise
        except ServiceNowRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ServiceNowError as exc:
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


def _extract_value(field: Any) -> str:
    """ServiceNow returns fields as either a plain string or {'value': ..., 'display_value': ...}."""
    if isinstance(field, dict):
        return str(field.get("value", "") or field.get("display_value", "") or "")
    return str(field) if field is not None else ""


def normalize_incident(
    incident: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    instance: str,
) -> ConnectorDocument:
    """Convert a raw ServiceNow incident record into a ConnectorDocument.

    The source_id is SHA-256("incident:" + sys_id)[:16] — stable and collision-resistant.
    """
    sys_id: str = _extract_value(incident.get("sys_id", ""))
    number: str = _extract_value(incident.get("number", ""))
    short_desc: str = _extract_value(incident.get("short_description", ""))
    description: str = _extract_value(incident.get("description", ""))
    state: str = _extract_value(incident.get("state", ""))
    priority: str = _extract_value(incident.get("priority", ""))
    urgency: str = _extract_value(incident.get("urgency", ""))
    impact: str = _extract_value(incident.get("impact", ""))
    category: str = _extract_value(incident.get("category", ""))
    assigned_to: str = _extract_value(incident.get("assigned_to", ""))
    caller_id: str = _extract_value(incident.get("caller_id", ""))
    opened_at: str = _extract_value(incident.get("opened_at", ""))
    resolved_at: str = _extract_value(incident.get("resolved_at", ""))
    closed_at: str = _extract_value(incident.get("closed_at", ""))
    sys_updated_on: str = _extract_value(incident.get("sys_updated_on", ""))

    title = f"Incident {number}: {short_desc}" if short_desc else f"Incident {number}"
    content_parts = [short_desc, description]
    content = "\n\n".join(part for part in content_parts if part)

    source_id = _short_hash(f"incident:{sys_id}")
    source_url = (
        f"https://{instance}.service-now.com/incident.do?sys_id={sys_id}"
        if sys_id else ""
    )

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "sys_id": sys_id,
            "number": number,
            "state": state,
            "priority": priority,
            "urgency": urgency,
            "impact": impact,
            "category": category,
            "assigned_to": assigned_to,
            "caller_id": caller_id,
            "opened_at": opened_at,
            "resolved_at": resolved_at,
            "closed_at": closed_at,
            "sys_updated_on": sys_updated_on,
            "record_type": "incident",
        },
    )


def normalize_change(
    change: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    instance: str,
) -> ConnectorDocument:
    """Convert a raw ServiceNow change_request record into a ConnectorDocument.

    The source_id is SHA-256("change:" + sys_id)[:16].
    """
    sys_id: str = _extract_value(change.get("sys_id", ""))
    number: str = _extract_value(change.get("number", ""))
    short_desc: str = _extract_value(change.get("short_description", ""))
    description: str = _extract_value(change.get("description", ""))
    state: str = _extract_value(change.get("state", ""))
    priority: str = _extract_value(change.get("priority", ""))
    risk: str = _extract_value(change.get("risk", ""))
    change_type: str = _extract_value(change.get("type", ""))
    assigned_to: str = _extract_value(change.get("assigned_to", ""))
    requested_by: str = _extract_value(change.get("requested_by", ""))
    start_date: str = _extract_value(change.get("start_date", ""))
    end_date: str = _extract_value(change.get("end_date", ""))
    sys_updated_on: str = _extract_value(change.get("sys_updated_on", ""))

    title = (
        f"Change Request {number}: {short_desc}"
        if short_desc
        else f"Change Request {number}"
    )
    content_parts = [short_desc, description]
    content = "\n\n".join(part for part in content_parts if part)

    source_id = _short_hash(f"change:{sys_id}")
    source_url = (
        f"https://{instance}.service-now.com/change_request.do?sys_id={sys_id}"
        if sys_id else ""
    )

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "sys_id": sys_id,
            "number": number,
            "state": state,
            "priority": priority,
            "risk": risk,
            "change_type": change_type,
            "assigned_to": assigned_to,
            "requested_by": requested_by,
            "start_date": start_date,
            "end_date": end_date,
            "sys_updated_on": sys_updated_on,
            "record_type": "change_request",
        },
    )
