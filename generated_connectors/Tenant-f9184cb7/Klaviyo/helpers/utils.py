from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import KlaviyoAuthError, KlaviyoError, KlaviyoRateLimitError
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
    last_exc: KlaviyoError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except KlaviyoAuthError:
            raise
        except KlaviyoRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except KlaviyoError as exc:
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
    """Return SHA-256(raw_id)[:16] as a stable document identifier."""
    return hashlib.sha256(raw_id.encode()).hexdigest()[:16]


def normalize_profile(profile: dict[str, Any], connector_id: str, tenant_id: str) -> ConnectorDocument:
    """Convert a Klaviyo JSON:API profile resource into a ConnectorDocument.

    Handles both wrapped JSON:API format ({"data": {...}}) and the raw attributes
    dict that may appear during pagination unwrapping.
    """
    # JSON:API: profile may be the top-level response or a list item
    if "data" in profile and isinstance(profile["data"], dict):
        resource = profile["data"]
    elif "type" in profile and profile.get("type") == "profile":
        resource = profile
    else:
        resource = profile

    profile_id: str = resource.get("id", "")
    attrs: dict[str, Any] = resource.get("attributes", {})

    email = attrs.get("email", "")
    first_name = attrs.get("first_name", "")
    last_name = attrs.get("last_name", "")
    phone = attrs.get("phone_number", "")
    created = attrs.get("created", "")
    updated = attrs.get("updated", "")

    full_name = f"{first_name} {last_name}".strip() or "Unknown"
    title = f"Klaviyo profile: {full_name} <{email}>" if email else f"Klaviyo profile: {full_name}"

    content_parts = [
        f"Profile ID: {profile_id}",
        f"Name: {full_name}",
        f"Email: {email}",
    ]
    if phone:
        content_parts.append(f"Phone: {phone}")
    if created:
        content_parts.append(f"Created: {created}")
    if updated:
        content_parts.append(f"Updated: {updated}")

    location = attrs.get("location", {}) or {}
    if location:
        city = location.get("city", "")
        region = location.get("region", "")
        country = location.get("country", "")
        loc_str = ", ".join(filter(None, [city, region, country]))
        if loc_str:
            content_parts.append(f"Location: {loc_str}")

    return ConnectorDocument(
        source_id=_stable_id(profile_id) if profile_id else profile_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://www.klaviyo.com/profile/{profile_id}",
        metadata={
            "profile_id": profile_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "phone_number": phone,
            "created": created,
            "updated": updated,
            "location": location,
        },
    )


def normalize_campaign(campaign: dict[str, Any], connector_id: str, tenant_id: str) -> ConnectorDocument:
    """Convert a Klaviyo JSON:API campaign resource into a ConnectorDocument."""
    if "data" in campaign and isinstance(campaign["data"], dict):
        resource = campaign["data"]
    elif "type" in campaign and campaign.get("type") == "campaign":
        resource = campaign
    else:
        resource = campaign

    campaign_id: str = resource.get("id", "")
    attrs: dict[str, Any] = resource.get("attributes", {})

    name = attrs.get("name", "Unnamed Campaign")
    status = attrs.get("status", "unknown")
    send_time = attrs.get("scheduled_at", "") or attrs.get("send_time", "")
    created = attrs.get("created_at", "")
    updated = attrs.get("updated_at", "")

    title = f"Klaviyo campaign: {name}"

    content_parts = [
        f"Campaign ID: {campaign_id}",
        f"Name: {name}",
        f"Status: {status}",
    ]
    if send_time:
        content_parts.append(f"Scheduled at: {send_time}")
    if created:
        content_parts.append(f"Created: {created}")
    if updated:
        content_parts.append(f"Updated: {updated}")

    return ConnectorDocument(
        source_id=_stable_id(campaign_id) if campaign_id else campaign_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://www.klaviyo.com/campaign/{campaign_id}",
        metadata={
            "campaign_id": campaign_id,
            "name": name,
            "status": status,
            "scheduled_at": send_time,
            "created_at": created,
            "updated_at": updated,
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
