from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import PagerDutyAuthError, PagerDutyError, PagerDutyRateLimitError
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

    Auth errors (PagerDutyAuthError) are never retried — they require human
    intervention.  Rate-limit errors honour the retry_after value when present.
    """
    last_exc: PagerDutyError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except PagerDutyAuthError:
            raise
        except PagerDutyRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except PagerDutyError as exc:
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


def normalize_incident(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw PagerDuty incident into a ConnectorDocument.

    ``source_id`` is a stable 16-char SHA-256 prefix of ``"incident:<id>"``.
    """
    incident_id: str = str(raw.get("id", ""))
    title: str = raw.get("title", "") or f"Incident {incident_id}"
    status: str = raw.get("status", "")
    urgency: str = raw.get("urgency", "")
    description: str = raw.get("description", "") or ""
    incident_number: int | None = raw.get("incident_number")
    created_at: str = raw.get("created_at", "")
    updated_at: str = raw.get("updated_at", "")
    html_url: str = raw.get("html_url", "")

    service_obj: dict[str, Any] = raw.get("service", {}) or {}
    service_name: str = service_obj.get("summary", "")

    assignee_list: list[dict[str, Any]] = raw.get("assignments", [])
    assignees: list[str] = [
        a.get("assignee", {}).get("summary", "") for a in assignee_list if a
    ]

    content_parts = [f"Status: {status}", f"Urgency: {urgency}"]
    if description:
        content_parts.append(f"Description: {description}")
    if service_name:
        content_parts.append(f"Service: {service_name}")
    if assignees:
        content_parts.append(f"Assigned to: {', '.join(a for a in assignees if a)}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"incident:{incident_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Incident #{incident_number}: {title}" if incident_number else title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=html_url,
        metadata={
            "incident_id": incident_id,
            "incident_number": incident_number,
            "status": status,
            "urgency": urgency,
            "service": service_name,
            "assignees": assignees,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_service(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw PagerDuty service into a ConnectorDocument.

    ``source_id`` is a stable 16-char SHA-256 prefix of ``"service:<id>"``.
    """
    service_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "") or f"Service {service_id}"
    description: str = raw.get("description", "") or ""
    status: str = raw.get("status", "")
    html_url: str = raw.get("html_url", "")
    created_at: str = raw.get("created_at", "")
    updated_at: str = raw.get("updated_at", "")

    team_obj: dict[str, Any] = raw.get("team", {}) or {}
    team_name: str = team_obj.get("summary", "")

    content_parts = [f"Status: {status}"]
    if description:
        content_parts.append(f"Description: {description}")
    if team_name:
        content_parts.append(f"Team: {team_name}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"service:{service_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=html_url,
        metadata={
            "service_id": service_id,
            "status": status,
            "team": team_name,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_user(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw PagerDuty user into a ConnectorDocument."""
    user_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "") or f"User {user_id}"
    email: str = raw.get("email", "")
    role: str = raw.get("role", "")
    job_title: str = raw.get("job_title", "") or ""
    html_url: str = raw.get("html_url", "")
    time_zone: str = raw.get("time_zone", "")

    content_parts = [f"Name: {name}", f"Email: {email}", f"Role: {role}"]
    if job_title:
        content_parts.append(f"Title: {job_title}")
    if time_zone:
        content_parts.append(f"Timezone: {time_zone}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"user:{user_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=html_url,
        metadata={
            "user_id": user_id,
            "email": email,
            "role": role,
            "job_title": job_title,
            "time_zone": time_zone,
        },
    )


def normalize_schedule(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw PagerDuty schedule into a ConnectorDocument."""
    schedule_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "") or f"Schedule {schedule_id}"
    description: str = raw.get("description", "") or ""
    time_zone: str = raw.get("time_zone", "")
    html_url: str = raw.get("html_url", "")

    content_parts = [f"Schedule: {name}"]
    if time_zone:
        content_parts.append(f"Timezone: {time_zone}")
    if description:
        content_parts.append(f"Description: {description}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"schedule:{schedule_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=html_url,
        metadata={
            "schedule_id": schedule_id,
            "time_zone": time_zone,
            "description": description,
        },
    )


def normalize_team(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw PagerDuty team into a ConnectorDocument."""
    team_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "") or f"Team {team_id}"
    description: str = raw.get("description", "") or ""
    html_url: str = raw.get("html_url", "")

    content_parts = [f"Team: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"team:{team_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=html_url,
        metadata={
            "team_id": team_id,
            "description": description,
        },
    )
