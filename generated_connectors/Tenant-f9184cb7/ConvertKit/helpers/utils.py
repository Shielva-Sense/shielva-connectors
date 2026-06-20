from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ConvertKitAuthError, ConvertKitError, ConvertKitRateLimitError
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

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: ConvertKitError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ConvertKitAuthError:
            raise
        except ConvertKitRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ConvertKitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_id(prefix: str, raw_id: str | int) -> str:
    """Return SHA-256(prefix+str(raw_id))[:16] as a stable document identifier."""
    key = f"{prefix}{raw_id}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def normalize_subscriber(s: dict[str, Any], connector_id: str = "", tenant_id: str = "") -> ConnectorDocument:
    """Convert a ConvertKit subscriber dict into a ConnectorDocument.

    Stable ID: sha256("subscriber:" + str(s["id"]))[:16]
    Document type: "subscriber"
    """
    sub_id = s.get("id", "")
    email = s.get("email_address", s.get("email", ""))
    first_name = s.get("first_name", "")
    state = s.get("state", "")
    created = s.get("created_at", "")

    doc_id = _stable_id("subscriber:", sub_id)
    title = f"ConvertKit subscriber: {first_name} <{email}>" if first_name else f"ConvertKit subscriber: {email}"

    content_parts = [f"Subscriber ID: {sub_id}", f"Email: {email}"]
    if first_name:
        content_parts.append(f"First name: {first_name}")
    if state:
        content_parts.append(f"State: {state}")
    if created:
        content_parts.append(f"Created: {created}")

    fields = s.get("fields", {}) or {}
    if fields:
        for k, v in fields.items():
            if v:
                content_parts.append(f"{k}: {v}")

    return ConnectorDocument(
        source_id=doc_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.convertkit.com/subscribers/{sub_id}",
        metadata={
            "type": "subscriber",
            "subscriber_id": sub_id,
            "email": email,
            "first_name": first_name,
            "state": state,
            "created_at": created,
            "fields": fields,
        },
    )


def normalize_sequence(s: dict[str, Any], connector_id: str = "", tenant_id: str = "") -> ConnectorDocument:
    """Convert a ConvertKit sequence dict into a ConnectorDocument.

    Stable ID: sha256("sequence:" + str(s["id"]))[:16]
    Document type: "sequence"
    """
    seq_id = s.get("id", "")
    name = s.get("name", "Unnamed Sequence")
    hold = s.get("hold", False)
    repeat = s.get("repeat", False)
    created = s.get("created_at", "")

    doc_id = _stable_id("sequence:", seq_id)
    title = f"ConvertKit sequence: {name}"

    content_parts = [f"Sequence ID: {seq_id}", f"Name: {name}"]
    content_parts.append(f"Hold: {hold}")
    content_parts.append(f"Repeat: {repeat}")
    if created:
        content_parts.append(f"Created: {created}")

    return ConnectorDocument(
        source_id=doc_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.convertkit.com/sequences/{seq_id}",
        metadata={
            "type": "sequence",
            "sequence_id": seq_id,
            "name": name,
            "hold": hold,
            "repeat": repeat,
            "created_at": created,
        },
    )


def normalize_form(f: dict[str, Any], connector_id: str = "", tenant_id: str = "") -> ConnectorDocument:
    """Convert a ConvertKit form dict into a ConnectorDocument.

    Stable ID: sha256("form:" + str(f["id"]))[:16]
    Document type: "form"
    """
    form_id = f.get("id", "")
    name = f.get("name", "Unnamed Form")
    form_type = f.get("type", "")
    url = f.get("url", "")
    embed_url = f.get("embed_url", "")
    created = f.get("created_at", "")

    doc_id = _stable_id("form:", form_id)
    title = f"ConvertKit form: {name}"

    content_parts = [f"Form ID: {form_id}", f"Name: {name}"]
    if form_type:
        content_parts.append(f"Type: {form_type}")
    if url:
        content_parts.append(f"URL: {url}")
    if embed_url:
        content_parts.append(f"Embed URL: {embed_url}")
    if created:
        content_parts.append(f"Created: {created}")

    return ConnectorDocument(
        source_id=doc_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=url or f"https://app.convertkit.com/forms/{form_id}",
        metadata={
            "type": "form",
            "form_id": form_id,
            "name": name,
            "form_type": form_type,
            "url": url,
            "embed_url": embed_url,
            "created_at": created,
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
            import time
            if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                self._state = "half-open"
        return self._state

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            import time
            self._state = "open"
            self._opened_at = time.monotonic()

    def on_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    @property
    def is_open(self) -> bool:
        return self.state == "open"
