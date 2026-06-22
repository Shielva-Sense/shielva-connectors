from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import AcuityAuthError, AcuityError, AcuityRateLimitError
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
    last_exc: AcuityError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except AcuityAuthError:
            raise
        except AcuityRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except AcuityError as exc:
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


def normalize_appointment(
    appointment: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Acuity appointment into a ConnectorDocument.

    source_id = SHA-256("appointment:" + str(appointment["id"]))[:16]
    """
    appt_id: int | str = appointment.get("id", "")
    appt_type: str = appointment.get("type", "") or "Appointment"
    first_name: str = appointment.get("firstName", "") or ""
    last_name: str = appointment.get("lastName", "") or ""
    email: str = appointment.get("email", "") or ""
    phone: str = appointment.get("phone", "") or ""
    date: str = appointment.get("date", "") or ""
    time: str = appointment.get("time", "") or ""
    end_time: str = appointment.get("endTime", "") or ""
    calendar: str = appointment.get("calendar", "") or ""
    status: str = appointment.get("status", "") or ""
    notes: str = appointment.get("notes", "") or ""
    location: str = appointment.get("location", "") or ""
    timezone: str = appointment.get("timezone", "") or ""

    full_name = f"{first_name} {last_name}".strip() or email or "Unknown Client"

    content_parts: list[str] = [
        f"Appointment Type: {appt_type}",
        f"Client: {full_name}",
    ]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if date:
        content_parts.append(f"Date: {date}")
    if time:
        content_parts.append(f"Time: {time}")
    if end_time:
        content_parts.append(f"End Time: {end_time}")
    if calendar:
        content_parts.append(f"Calendar: {calendar}")
    if status:
        content_parts.append(f"Status: {status}")
    if location:
        content_parts.append(f"Location: {location}")
    if timezone:
        content_parts.append(f"Timezone: {timezone}")
    if notes:
        content_parts.append(f"Notes: {notes}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"appointment:{appt_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"{appt_type} — {full_name} on {date}",
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "id": appt_id,
            "type": appt_type,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "date": date,
            "time": time,
            "end_time": end_time,
            "calendar": calendar,
            "status": status,
            "location": location,
            "timezone": timezone,
        },
    )


def normalize_client(
    client: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Acuity client into a ConnectorDocument.

    source_id = SHA-256("client:" + str(client["id"]))[:16]
    """
    client_id: int | str = client.get("id", "")
    first_name: str = client.get("firstName", "") or ""
    last_name: str = client.get("lastName", "") or ""
    email: str = client.get("email", "") or ""
    phone: str = client.get("phone", "") or ""
    notes: str = client.get("notes", "") or ""

    full_name = f"{first_name} {last_name}".strip() or email or "Unknown Client"

    content_parts: list[str] = [f"Client: {full_name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if notes:
        content_parts.append(f"Notes: {notes}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"client:{client_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Client: {full_name}",
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "id": client_id,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
        },
    )


def normalize_appointment_type(
    appt_type: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Acuity appointment type into a ConnectorDocument.

    source_id = SHA-256("appointment_type:" + str(appt_type["id"]))[:16]
    """
    type_id: int | str = appt_type.get("id", "")
    name: str = appt_type.get("name", "") or "Appointment Type"
    duration: int = appt_type.get("duration", 0)
    price: str = appt_type.get("price", "") or ""
    category: str = appt_type.get("category", "") or ""
    description: str = appt_type.get("description", "") or ""
    color: str = appt_type.get("color", "") or ""
    active: bool = appt_type.get("active", True)

    content_parts: list[str] = [
        f"Appointment Type: {name}",
        f"Duration: {duration} minutes",
        f"Active: {active}",
    ]
    if price:
        content_parts.append(f"Price: {price}")
    if category:
        content_parts.append(f"Category: {category}")
    if description:
        content_parts.append(f"Description: {description}")
    if color:
        content_parts.append(f"Color: {color}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"appointment_type:{type_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Appointment Type: {name}",
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "id": type_id,
            "name": name,
            "duration": duration,
            "price": price,
            "category": category,
            "active": active,
        },
    )
