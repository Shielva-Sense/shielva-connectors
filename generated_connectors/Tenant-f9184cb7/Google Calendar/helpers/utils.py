"""Shared utilities: event normalization, retry logic with exponential backoff.

Imports only from stdlib and exceptions (bare module name — loaded by gateway
sys.path injection). No shared.* imports here to keep this module self-contained.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Callable, Coroutine, Dict, List, Optional

import structlog

from exceptions import GoogleCalendarRateLimitError, GoogleCalendarNetworkError
from models import ConnectorDocument

logger = structlog.get_logger(__name__)

# OCP: retry constants — change here, nowhere else
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> Any:
    """Execute *coro_fn()* with exponential-backoff retry.

    Retries on GoogleCalendarRateLimitError and GoogleCalendarNetworkError.
    Raises the last exception after exhausting all retries.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except GoogleCalendarRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "google_calendar.rate_limit — retrying",
                attempt=attempt + 1,
                delay=delay,
            )
            await asyncio.sleep(delay)
        except GoogleCalendarNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "google_calendar.network_error — retrying",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def _extract_datetime(dt_obj: Optional[Dict[str, Any]]) -> str:
    """Return the best date/time string from a Google Calendar dateTime/date object."""
    if not dt_obj:
        return ""
    return dt_obj.get("dateTime") or dt_obj.get("date") or ""


def _attendees_text(attendees: List[Dict[str, Any]]) -> str:
    """Format a list of attendee dicts into a human-readable string."""
    if not attendees:
        return ""
    parts: List[str] = []
    for att in attendees:
        email = att.get("email", "")
        name = att.get("displayName", "")
        status = att.get("responseStatus", "")
        part = name or email
        if status:
            part = f"{part} ({status})"
        if part:
            parts.append(part)
    return ", ".join(parts)


def normalize_event(
    event: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Google Calendar API event dict to a ConnectorDocument.

    Title    = event summary (falling back to "(no title)").
    Content  = description + formatted attendee list.
    Metadata = start/end datetime, location, organizer, status, calendar_id.
    """
    event_id: str = event.get("id", "")
    summary: str = event.get("summary", "") or "(no title)"
    description: str = event.get("description", "") or ""
    location: str = event.get("location", "") or ""
    status: str = event.get("status", "") or ""
    html_link: str = event.get("htmlLink", "") or ""

    start_raw: Optional[Dict[str, Any]] = event.get("start")
    end_raw: Optional[Dict[str, Any]] = event.get("end")
    start_dt: str = _extract_datetime(start_raw)
    end_dt: str = _extract_datetime(end_raw)

    organizer: Dict[str, Any] = event.get("organizer") or {}
    organizer_email: str = organizer.get("email", "") or ""
    organizer_name: str = organizer.get("displayName", "") or ""
    organizer_str: str = organizer_name or organizer_email

    attendees: List[Dict[str, Any]] = event.get("attendees") or []
    attendees_text: str = _attendees_text(attendees)

    # Build human-readable content block
    content_parts: List[str] = []
    if description:
        content_parts.append(description)
    if attendees_text:
        content_parts.append(f"Attendees: {attendees_text}")
    if location:
        content_parts.append(f"Location: {location}")
    if start_dt:
        content_parts.append(f"Start: {start_dt}")
    if end_dt:
        content_parts.append(f"End: {end_dt}")
    content: str = "\n".join(content_parts)

    metadata: Dict[str, Any] = {
        "start": start_dt,
        "end": end_dt,
        "location": location,
        "organizer": organizer_str,
        "organizer_email": organizer_email,
        "status": status,
        "attendees": attendees_text,
        "html_link": html_link,
        "recurring_event_id": event.get("recurringEventId", ""),
        "calendar_id": event.get("organizer", {}).get("email", "primary"),
        "etag": event.get("etag", ""),
    }

    return ConnectorDocument(
        id=f"{connector_id}_{event_id}",
        source="google_calendar",
        title=summary,
        content=content,
        metadata=metadata,
        connector_id=connector_id,
        tenant_id=tenant_id,
    )
