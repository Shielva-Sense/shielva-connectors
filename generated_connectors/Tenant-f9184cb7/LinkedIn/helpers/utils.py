from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import LinkedInAuthError, LinkedInError, LinkedInRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(entity_type: str, entity_id: str) -> str:
    """SHA-256(entity_type + ':' + entity_id)[:16] — stable, collision-resistant source ID."""
    raw = f"{entity_type}:{entity_id}"
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
    Rate-limit errors honour the retry_after value when > 0.
    """
    last_exc: LinkedInError | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except LinkedInAuthError:
            raise
        except LinkedInRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except LinkedInError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


class CircuitBreaker:
    """Simple three-state circuit breaker (closed -> open -> half-open -> closed)."""

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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _localized_string(obj: Any, preferred_locale: str = "en_US") -> str:
    """Extract a string from a LinkedIn localized object.

    LinkedIn returns names as:
      {"localized": {"en_US": "Value"}, "preferredLocale": {"country": "US", "language": "en"}}
    """
    if not isinstance(obj, dict):
        return str(obj) if obj else ""
    localized = obj.get("localized", {})
    if not isinstance(localized, dict):
        return ""
    # Try preferred locale first, then any available locale
    if preferred_locale in localized:
        return localized[preferred_locale]
    for value in localized.values():
        if value:
            return str(value)
    return ""


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_profile(
    profile: dict[str, Any],
    email_data: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert raw LinkedIn /me + /emailAddress responses into a ConnectorDocument."""
    person_id: str = profile.get("id", "") or ""
    source_id = _stable_id("profile", person_id)

    first_name_obj = profile.get("firstName", {}) or {}
    last_name_obj = profile.get("lastName", {}) or {}
    first_name = _localized_string(first_name_obj)
    last_name = _localized_string(last_name_obj)
    full_name = f"{first_name} {last_name}".strip() or person_id

    headline_obj = profile.get("headline", {}) or {}
    headline = _localized_string(headline_obj)

    # Extract email from nested elements structure
    email = ""
    elements: list[Any] = (email_data.get("elements", []) or [])
    if elements:
        first_element = elements[0] if isinstance(elements[0], dict) else {}
        handle_tilde = first_element.get("handle~", {}) or {}
        email = handle_tilde.get("emailAddress", "") or ""

    profile_url = f"https://www.linkedin.com/in/{person_id}" if person_id else ""

    content_parts = [
        f"LinkedIn Profile: {full_name}",
        f"Headline: {headline}" if headline else "",
        f"Email: {email}" if email else "",
        f"Person URN: urn:li:person:{person_id}" if person_id else "",
        f"Profile URL: {profile_url}" if profile_url else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"LinkedIn profile: {full_name}",
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=profile_url,
        metadata={
            "entity_type": "profile",
            "person_id": person_id,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "headline": headline,
            "email": email,
            "author_urn": f"urn:li:person:{person_id}" if person_id else "",
        },
    )


def normalize_post(
    record: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw LinkedIn share/post object into a ConnectorDocument.

    stable id = SHA-256('post:' + share_id)[:16]
    """
    share_id: str = record.get("id", "") or ""
    source_id = _stable_id("post", share_id)

    # Commentary / text
    commentary: str = ""
    specific_content = record.get("specificContent", {}) or {}
    share_content = specific_content.get("com.linkedin.ugc.ShareContent", {}) or {}
    commentary = share_content.get("shareCommentaryV2", {}) or {}
    if isinstance(commentary, dict):
        commentary = commentary.get("text", "") or ""
    if not commentary:
        # Fallback: top-level text field (Shares API v2)
        commentary = (record.get("text", {}) or {}).get("text", "") or ""

    author_urn: str = record.get("author", "") or ""
    created_at: str = ""
    last_modified: str = ""
    activity = record.get("activity", "") or ""

    # Timestamps may be epoch millis
    created_obj = record.get("created", {}) or {}
    last_mod_obj = record.get("lastModified", {}) or {}
    if isinstance(created_obj, dict):
        ts = created_obj.get("time", 0) or 0
        created_at = str(ts) if ts else ""
    if isinstance(last_mod_obj, dict):
        ts = last_mod_obj.get("time", 0) or 0
        last_modified = str(ts) if ts else ""

    visibility = (record.get("visibility", {}) or {}).get("com.linkedin.ugc.MemberNetworkVisibility", "") or ""

    source_url = f"https://www.linkedin.com/feed/update/urn:li:share:{share_id}/" if share_id else ""

    content_parts = [
        f"LinkedIn Post by {author_urn}",
        f"Text: {commentary}" if commentary else "",
        f"Author URN: {author_urn}" if author_urn else "",
        f"Activity: {activity}" if activity else "",
        f"Visibility: {visibility}" if visibility else "",
        f"Created: {created_at}" if created_at else "",
        f"Last Modified: {last_modified}" if last_modified else "",
        f"URL: {source_url}" if source_url else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"LinkedIn post: {commentary[:80]}..." if len(commentary) > 80 else f"LinkedIn post: {commentary or share_id}",
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "entity_type": "post",
            "share_id": share_id,
            "author_urn": author_urn,
            "text": commentary,
            "visibility": visibility,
            "created_at": created_at,
            "last_modified": last_modified,
            "activity": activity,
        },
    )
