from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import SalesloftAuthError, SalesloftError, SalesloftRateLimitError
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
    max_retries: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: SalesloftError | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except SalesloftAuthError:
            raise
        except SalesloftRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except SalesloftError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def stable_id(entity_type: str, entity_id: str | int) -> str:
    """Return a 16-character stable SHA-256 hex digest for a Salesloft entity."""
    raw = f"{entity_type}:{entity_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class CircuitBreaker:
    """Simple three-state circuit breaker (closed → open → half-open → closed)."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self._failures: int = 0
        self._state: str = "closed"
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                self._state = "half-open"
        return self._state

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()

    def on_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    @property
    def is_open(self) -> bool:
        return self.state == "open"


# ── Normalizers ──────────────────────────────────────────────────────────────


def normalize_person(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw Salesloft People object into a ConnectorDocument."""
    person_id = str(record.get("id", ""))
    first_name = str(record.get("first_name", "") or "")
    last_name = str(record.get("last_name", "") or "")
    full_name = f"{first_name} {last_name}".strip() or f"Person {person_id}"
    email = str(record.get("email_address", "") or "")
    title = str(record.get("title", "") or "")
    company = str(record.get("company_name", "") or "")
    phone = str(record.get("phone", "") or "")
    city = str(record.get("city", "") or "")
    state = str(record.get("state", "") or "")
    country = str(record.get("country", "") or "")
    created_at = str(record.get("created_at", "") or "")
    updated_at = str(record.get("updated_at", "") or "")
    crm_id = str(record.get("crm_id", "") or "")

    doc_title = f"Salesloft person: {full_name}" + (f" <{email}>" if email else "")
    content_parts = [
        f"Person ID: {person_id}",
        f"Name: {full_name}",
        f"Email: {email}",
        f"Title: {title}",
        f"Company: {company}",
        f"Phone: {phone}",
        f"City: {city}",
        f"State: {state}",
        f"Country: {country}",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
    ]

    src_id = stable_id("person", person_id)
    return ConnectorDocument(
        source_id=src_id,
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.salesloft.com/app/people/{person_id}",
        metadata={
            "object_type": "person",
            "person_id": person_id,
            "name": full_name,
            "email": email,
            "title": title,
            "company": company,
            "phone": phone,
            "city": city,
            "state": state,
            "country": country,
            "crm_id": crm_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_cadence(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw Salesloft Cadence object into a ConnectorDocument."""
    cadence_id = str(record.get("id", ""))
    name = str(record.get("name", "") or f"Cadence {cadence_id}")
    cadence_type = str(record.get("cadence_function", "") or "")
    tags = record.get("tags", []) or []
    tags_str = ", ".join(str(t) for t in tags) if tags else ""
    created_at = str(record.get("created_at", "") or "")
    updated_at = str(record.get("updated_at", "") or "")
    owner_guid = str(record.get("owner_guid", "") or "")
    draft = bool(record.get("draft", False))
    shared = bool(record.get("shared", False))
    archived_at = str(record.get("archived_at", "") or "")

    doc_title = f"Salesloft cadence: {name}"
    content_parts = [
        f"Cadence ID: {cadence_id}",
        f"Name: {name}",
        f"Type: {cadence_type}",
        f"Tags: {tags_str}",
        f"Draft: {draft}",
        f"Shared: {shared}",
        f"Owner: {owner_guid}",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
        f"Archived: {archived_at}",
    ]

    src_id = stable_id("cadence", cadence_id)
    return ConnectorDocument(
        source_id=src_id,
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.salesloft.com/app/cadence/{cadence_id}",
        metadata={
            "object_type": "cadence",
            "cadence_id": cadence_id,
            "name": name,
            "cadence_type": cadence_type,
            "tags": tags_str,
            "draft": str(draft),
            "shared": str(shared),
            "owner_guid": owner_guid,
            "created_at": created_at,
            "updated_at": updated_at,
            "archived_at": archived_at,
        },
    )


def normalize_call(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw Salesloft Call activity into a ConnectorDocument."""
    call_id = str(record.get("id", ""))
    duration = str(record.get("duration", "") or "")
    disposition = str(record.get("disposition", "") or "")
    sentiment = str(record.get("sentiment", "") or "")
    direction = str(record.get("direction", "") or "")
    created_at = str(record.get("created_at", "") or "")
    updated_at = str(record.get("updated_at", "") or "")
    to_number = str(record.get("to_number", "") or "")
    from_number = str(record.get("from_number", "") or "")
    recording_url = str(record.get("recording_url", "") or "")
    notes = str(record.get("notes", "") or "")

    doc_title = f"Salesloft call: {call_id}" + (f" ({disposition})" if disposition else "")
    content_parts = [
        f"Call ID: {call_id}",
        f"Duration: {duration}s",
        f"Disposition: {disposition}",
        f"Sentiment: {sentiment}",
        f"Direction: {direction}",
        f"To: {to_number}",
        f"From: {from_number}",
        f"Notes: {notes}",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
    ]

    src_id = stable_id("call", call_id)
    return ConnectorDocument(
        source_id=src_id,
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=recording_url or f"https://app.salesloft.com/app/activities/calls/{call_id}",
        metadata={
            "object_type": "call",
            "call_id": call_id,
            "duration": duration,
            "disposition": disposition,
            "sentiment": sentiment,
            "direction": direction,
            "to_number": to_number,
            "from_number": from_number,
            "recording_url": recording_url,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
