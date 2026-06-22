from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import CalendlyAuthError, CalendlyError, CalendlyRateLimitError
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
    last_exc: CalendlyError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except CalendlyAuthError:
            raise
        except CalendlyRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except CalendlyError as exc:
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


def _extract_uuid_from_uri(uri: str) -> str:
    """Extract the last path segment (UUID) from a Calendly resource URI."""
    return uri.rstrip("/").split("/")[-1]


def normalize_event_type(et: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Calendly event type into a ConnectorDocument.

    id = sha256("event_type:" + uuid)[:16], source="calendly", type="event_type"
    """
    uri: str = et.get("uri", "")
    uuid = _extract_uuid_from_uri(uri) if uri else ""
    source_id = _short_hash("event_type:" + uuid) if uuid else _short_hash("event_type:" + et.get("name", ""))

    name: str = et.get("name", "") or "Event Type"
    active: bool = et.get("active", False)
    duration: int = et.get("duration", 0)
    scheduling_url: str = et.get("scheduling_url", "")
    description_plain: str = et.get("description_plain", "") or et.get("description", "") or ""
    color: str = et.get("color", "")
    kind: str = et.get("kind", "")

    content_parts: list[str] = [
        f"Event Type: {name}",
        f"Active: {active}",
        f"Duration: {duration} minutes",
    ]
    if description_plain:
        content_parts.append(f"Description: {description_plain}")
    if scheduling_url:
        content_parts.append(f"Scheduling URL: {scheduling_url}")
    if kind:
        content_parts.append(f"Kind: {kind}")
    if color:
        content_parts.append(f"Color: {color}")

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=scheduling_url or uri,
        metadata={
            "uri": uri,
            "uuid": uuid,
            "active": active,
            "duration": duration,
            "scheduling_url": scheduling_url,
            "kind": kind,
            "color": color,
        },
    )


def normalize_scheduled_event(ev: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Calendly scheduled event into a ConnectorDocument.

    id = sha256("scheduled_event:" + uuid)[:16], source="calendly", type="scheduled_event"
    """
    uri: str = ev.get("uri", "")
    uuid = _extract_uuid_from_uri(uri) if uri else ""
    source_id = _short_hash("scheduled_event:" + uuid) if uuid else _short_hash("scheduled_event:" + ev.get("name", ""))

    name: str = ev.get("name", "") or "Scheduled Event"
    status: str = ev.get("status", "")
    start_time: str = ev.get("start_time", "")
    end_time: str = ev.get("end_time", "")
    location: dict[str, Any] = ev.get("location", {}) or {}
    location_type: str = location.get("type", "")
    location_join_url: str = location.get("join_url", "")
    created_at: str = ev.get("created_at", "")
    updated_at: str = ev.get("updated_at", "")
    event_type_uri: str = ev.get("event_type", "") or ""

    content_parts: list[str] = [
        f"Event: {name}",
        f"Status: {status}",
        f"Start: {start_time}",
        f"End: {end_time}",
    ]
    if location_type:
        loc_text = location_type
        if location_join_url:
            loc_text += f" ({location_join_url})"
        content_parts.append(f"Location: {loc_text}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"{name} — {start_time}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=uri,
        metadata={
            "uri": uri,
            "uuid": uuid,
            "event_type_uri": event_type_uri,
            "status": status,
            "start_time": start_time,
            "end_time": end_time,
            "location_type": location_type,
            "location_join_url": location_join_url,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_event(
    event: dict[str, Any],
    invitees: list[dict[str, Any]],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw Calendly scheduled event + invitees into a ConnectorDocument.

    The source_id is SHA-256(event_uri)[:16] — deterministic and collision-resistant.
    Calendly uses full URIs as IDs; we extract them via the stable hash.
    """
    event_uri: str = event.get("uri", "")
    event_name: str = event.get("name", "") or "Scheduled Event"
    status: str = event.get("status", "")
    start_time: str = event.get("start_time", "")
    end_time: str = event.get("end_time", "")
    location: dict[str, Any] = event.get("location", {}) or {}
    location_type: str = location.get("type", "")
    location_join_url: str = location.get("join_url", "")
    created_at: str = event.get("created_at", "")
    updated_at: str = event.get("updated_at", "")
    event_type_uri: str = event.get("event_type", "") or ""
    guests: list[dict[str, Any]] = event.get("event_guests", []) or []

    # Build readable content
    content_parts: list[str] = [
        f"Event: {event_name}",
        f"Status: {status}",
        f"Start: {start_time}",
        f"End: {end_time}",
    ]
    if location_type:
        loc_text = location_type
        if location_join_url:
            loc_text += f" ({location_join_url})"
        content_parts.append(f"Location: {loc_text}")

    if invitees:
        invitee_lines = []
        for inv in invitees:
            inv_name = inv.get("name", "")
            email = inv.get("email", "")
            inv_status = inv.get("status", "")
            invitee_lines.append(
                f"  - {inv_name} <{email}> [{inv_status}]" if inv_name else f"  - {email} [{inv_status}]"
            )
        content_parts.append("Invitees:\n" + "\n".join(invitee_lines))

    if guests:
        guest_lines = [f"  - {g.get('email', '')}" for g in guests]
        content_parts.append("Guests:\n" + "\n".join(guest_lines))

    content = "\n".join(content_parts)

    source_id = _short_hash(event_uri) if event_uri else _short_hash(event_name + start_time)

    return ConnectorDocument(
        source_id=source_id,
        title=f"{event_name} — {start_time}",
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=event_uri,
        metadata={
            "event_uri": event_uri,
            "event_type_uri": event_type_uri,
            "status": status,
            "start_time": start_time,
            "end_time": end_time,
            "location_type": location_type,
            "location_join_url": location_join_url,
            "invitee_count": len(invitees),
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
