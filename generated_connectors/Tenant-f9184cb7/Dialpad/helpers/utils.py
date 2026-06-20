from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import DialpadAuthError, DialpadError, DialpadRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(prefix: str, resource_id: str) -> str:
    """Return SHA-256(prefix + resource_id)[:16] as a stable document ID."""
    raw = f"{prefix}{resource_id}"
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
    """
    last_exc: DialpadError | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except DialpadAuthError:
            raise
        except DialpadRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except DialpadError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def normalize_call_log(
    c: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Dialpad call log object into a ConnectorDocument."""
    call_id = str(c.get("id", ""))
    direction = c.get("direction", "") or ""
    duration = c.get("duration", 0) or 0
    started_at = c.get("date_started", "") or c.get("started_at", "") or ""
    ended_at = c.get("date_ended", "") or c.get("ended_at", "") or ""
    from_number = c.get("from_number", "") or ""
    to_number = c.get("to_number", "") or ""
    status = c.get("state", "") or c.get("status", "") or ""
    target = c.get("target", {}) or {}
    target_name = target.get("name", "") or "" if isinstance(target, dict) else ""

    title = f"Dialpad call: {call_id}"
    content_parts = [
        f"Call ID: {call_id}",
        f"Direction: {direction}",
        f"Duration (s): {duration}",
        f"Started at: {started_at}",
        f"Ended at: {ended_at}",
        f"From: {from_number}",
        f"To: {to_number}",
        f"Status: {status}",
        f"Target: {target_name}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("call:", call_id),
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "call_log",
            "call_id": call_id,
            "direction": direction,
            "duration": duration,
            "started_at": started_at,
            "ended_at": ended_at,
            "from_number": from_number,
            "to_number": to_number,
            "status": status,
            "target_name": target_name,
        },
    )


def normalize_contact(
    c: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Dialpad contact object into a ConnectorDocument."""
    contact_id = str(c.get("id", ""))
    first_name = c.get("first_name", "") or ""
    last_name = c.get("last_name", "") or ""
    display_name = c.get("display_name", "") or f"{first_name} {last_name}".strip() or f"Contact {contact_id}"
    email = c.get("email", "") or ""
    phone = c.get("phone", "") or c.get("phones", [])
    if isinstance(phone, list):
        phone = phone[0] if phone else ""
    company = c.get("company", "") or ""
    job_title = c.get("job_title", "") or ""

    title = f"Dialpad contact: {display_name}"
    content_parts = [
        f"Contact ID: {contact_id}",
        f"Name: {display_name}",
        f"Email: {email}",
        f"Phone: {phone}",
        f"Company: {company}",
        f"Job title: {job_title}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("contact:", contact_id),
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "contact",
            "contact_id": contact_id,
            "display_name": display_name,
            "email": email,
            "phone": phone,
            "company": company,
            "job_title": job_title,
        },
    )


def normalize_user(
    u: dict[str, Any], connector_id: str = "", tenant_id: str = ""
) -> ConnectorDocument:
    """Convert a raw Dialpad user object into a ConnectorDocument."""
    user_id = str(u.get("id", ""))
    first_name = u.get("first_name", "") or ""
    last_name = u.get("last_name", "") or ""
    display_name = u.get("display_name", "") or f"{first_name} {last_name}".strip() or f"User {user_id}"
    email = u.get("email", "") or ""
    state = u.get("state", "") or ""
    office_id = str(u.get("office_id", "") or "")
    is_admin = u.get("is_admin", False)

    title = f"Dialpad user: {display_name}"
    content_parts = [
        f"User ID: {user_id}",
        f"Name: {display_name}",
        f"Email: {email}",
        f"State: {state}",
        f"Office ID: {office_id}",
        f"Admin: {is_admin}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("user:", user_id),
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "user",
            "user_id": user_id,
            "display_name": display_name,
            "email": email,
            "state": state,
            "office_id": office_id,
            "is_admin": is_admin,
        },
    )


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
