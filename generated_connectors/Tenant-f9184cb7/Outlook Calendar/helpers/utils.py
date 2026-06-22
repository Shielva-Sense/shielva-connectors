"""Outlook Calendar connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, Optional

from models import ConnectorDocument


def normalize_event(
    event: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Microsoft Graph event object into a ConnectorDocument."""
    event_id = event.get("id", "")
    stable_id = hashlib.sha256(
        f"{tenant_id}:{connector_id}:{event_id}".encode()
    ).hexdigest()[:32]

    subject = event.get("subject") or "(No subject)"
    start_raw = (event.get("start") or {}).get("dateTime", "")
    end_raw = (event.get("end") or {}).get("dateTime", "")
    start_tz = (event.get("start") or {}).get("timeZone", "UTC")
    is_all_day = event.get("isAllDay", False)

    attendees = event.get("attendees") or []
    attendee_emails = [
        (a.get("emailAddress") or {}).get("address", "")
        for a in attendees
        if (a.get("emailAddress") or {}).get("address")
    ]
    organizer = ((event.get("organizer") or {}).get("emailAddress") or {}).get("address", "")
    location = (event.get("location") or {}).get("displayName", "")
    online_link = (event.get("onlineMeeting") or {}).get("joinUrl", "")
    body_preview = event.get("bodyPreview") or ""

    content_parts = [f"Subject: {subject}"]
    if is_all_day:
        content_parts.append("All-day event")
    else:
        content_parts.append(f"Start: {start_raw} ({start_tz})")
        content_parts.append(f"End: {end_raw}")
    if organizer:
        content_parts.append(f"Organizer: {organizer}")
    if attendee_emails:
        content_parts.append(f"Attendees: {', '.join(attendee_emails)}")
    if location:
        content_parts.append(f"Location: {location}")
    if online_link:
        content_parts.append(f"Join URL: {online_link}")
    if body_preview:
        content_parts.append(f"Preview: {body_preview[:500]}")

    metadata: Dict[str, Any] = {
        "event_id": event_id,
        "subject": subject,
        "start_datetime": start_raw,
        "end_datetime": end_raw,
        "timezone": start_tz,
        "is_all_day": is_all_day,
        "organizer_email": organizer,
        "attendee_emails": attendee_emails,
        "attendee_count": len(attendees),
        "location": location,
        "online_meeting_url": online_link,
        "is_cancelled": event.get("isCancelled", False),
        "recurrence": bool(event.get("recurrence")),
        "web_link": event.get("webLink", ""),
        "calendar_id": event.get("calendarId", ""),
        "sensitivity": event.get("sensitivity", "normal"),
        "importance": event.get("importance", "normal"),
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "outlook_calendar",
    }

    return ConnectorDocument(
        id=stable_id,
        title=subject,
        content="\n".join(content_parts),
        metadata=metadata,
    )


async def with_retry(
    fn: Callable,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry."""
    from exceptions import OutlookCalendarAuthError, OutlookCalendarNetworkError, OutlookCalendarRateLimitError

    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                return await result
            return result
        except OutlookCalendarAuthError:
            raise
        except (OutlookCalendarNetworkError, OutlookCalendarRateLimitError) as exc:
            last_exc = exc
            if attempt < max_retries:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
