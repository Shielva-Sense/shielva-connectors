from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import CustomerIOAuthError, CustomerIOError, CustomerIORateLimitError
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
    last_exc: CustomerIOError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except CustomerIOAuthError:
            raise
        except CustomerIORateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except CustomerIOError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_id(raw_id: str) -> str:
    """Return SHA-256("customer:" + raw_id)[:16] as a stable document identifier."""
    return hashlib.sha256(f"customer:{raw_id}".encode()).hexdigest()[:16]


def _stable_id_plain(raw_id: str) -> str:
    """Return SHA-256(raw_id)[:16] — for non-customer resources."""
    return hashlib.sha256(raw_id.encode()).hexdigest()[:16]


def normalize_customer(
    customer: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Customer.io customer object into a ConnectorDocument.

    Customer.io search results nest customer data under a 'customer' key or
    return the customer directly depending on the endpoint.
    """
    # Unwrap if nested under 'customer' key
    if "customer" in customer and isinstance(customer["customer"], dict):
        resource = customer["customer"]
    else:
        resource = customer

    customer_id: str = str(resource.get("id", ""))
    email: str = resource.get("email", "")
    created_at: int | str = resource.get("created_at", 0)
    attributes: dict[str, Any] = resource.get("attributes", {}) or {}

    # Extract name from attributes if present
    first_name = str(attributes.get("first_name", "") or attributes.get("name", "")).strip()
    last_name = str(attributes.get("last_name", "")).strip()
    full_name = f"{first_name} {last_name}".strip() or ""

    if full_name and email:
        title = f"Customer.io customer: {full_name} <{email}>"
    elif email:
        title = f"Customer.io customer: {email}"
    elif full_name:
        title = f"Customer.io customer: {full_name}"
    else:
        title = f"Customer.io customer: {customer_id}"

    content_parts = [f"Customer ID: {customer_id}"]
    if email:
        content_parts.append(f"Email: {email}")
    if full_name:
        content_parts.append(f"Name: {full_name}")
    if created_at:
        content_parts.append(f"Created at: {created_at}")

    # Include up to 5 extra attributes in content for searchability
    extra_keys = [k for k in attributes if k not in ("first_name", "last_name", "name")]
    for key in extra_keys[:5]:
        val = attributes[key]
        if val is not None and val != "":
            content_parts.append(f"{key}: {val}")

    return ConnectorDocument(
        source_id=_stable_id(customer_id) if customer_id else customer_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://fly.customer.io/env/0/people/{customer_id}",
        metadata={
            "customer_id": customer_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "created_at": created_at,
            "attributes": attributes,
        },
    )


def normalize_campaign(
    campaign: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Customer.io campaign object into a ConnectorDocument."""
    campaign_id: int | str = campaign.get("id", "")
    name: str = campaign.get("name", "Unnamed Campaign")
    status: str = campaign.get("active", False) and "active" or "inactive"
    # active is a bool in Customer.io
    if isinstance(campaign.get("active"), bool):
        status = "active" if campaign["active"] else "inactive"
    created: int | str = campaign.get("created", "")
    updated: int | str = campaign.get("updated", "")
    msg_type: str = campaign.get("msg_type", "")
    tags: list[str] = campaign.get("tags", []) or []

    content_parts = [
        f"Campaign ID: {campaign_id}",
        f"Name: {name}",
        f"Status: {status}",
    ]
    if msg_type:
        content_parts.append(f"Message type: {msg_type}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    if created:
        content_parts.append(f"Created: {created}")
    if updated:
        content_parts.append(f"Updated: {updated}")

    return ConnectorDocument(
        source_id=_stable_id_plain(str(campaign_id)) if campaign_id else str(campaign_id),
        title=f"Customer.io campaign: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://fly.customer.io/env/0/campaigns/{campaign_id}",
        metadata={
            "campaign_id": campaign_id,
            "name": name,
            "status": status,
            "msg_type": msg_type,
            "tags": tags,
            "created": created,
            "updated": updated,
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
