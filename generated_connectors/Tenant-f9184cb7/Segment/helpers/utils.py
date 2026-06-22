from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import SegmentAuthError, SegmentError, SegmentRateLimitError
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
    last_exc: SegmentError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except SegmentAuthError:
            raise
        except SegmentRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except SegmentError as exc:
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
    """Return SHA-256('source:' + raw_id)[:16] as a stable document identifier."""
    return hashlib.sha256(f"source:{raw_id}".encode()).hexdigest()[:16]


def normalize_source(source: dict[str, Any], connector_id: str, tenant_id: str) -> ConnectorDocument:
    """Convert a Segment source object into a ConnectorDocument.

    Segment source shape:
    {
        "id": "...",
        "slug": "...",
        "name": "...",
        "enabled": true,
        "workspaceId": "...",
        "writeKey": "...",
        "metadata": { "name": "...", "slug": "...", "description": "...", "categories": [...] },
        "settings": {}
    }
    """
    source_id: str = source.get("id", "")
    name: str = source.get("name", "") or source.get("slug", "Unnamed Source")
    slug: str = source.get("slug", "")
    enabled: bool = bool(source.get("enabled", False))
    workspace_id: str = source.get("workspaceId", "")
    write_key: str = source.get("writeKey", "")

    meta: dict[str, Any] = source.get("metadata", {}) or {}
    meta_name: str = meta.get("name", "")
    meta_description: str = meta.get("description", "")
    categories: list[str] = meta.get("categories", []) or []

    display_name = name or meta_name or slug or "Unnamed Source"
    title = f"Segment source: {display_name}"

    content_parts = [
        f"Source ID: {source_id}",
        f"Name: {display_name}",
        f"Slug: {slug}",
        f"Enabled: {enabled}",
    ]
    if workspace_id:
        content_parts.append(f"Workspace ID: {workspace_id}")
    if meta_description:
        content_parts.append(f"Description: {meta_description}")
    if categories:
        content_parts.append(f"Categories: {', '.join(categories)}")

    return ConnectorDocument(
        source_id=_stable_id(source_id) if source_id else source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.segment.com/goto-my-workspace/sources/{slug}/overview",
        metadata={
            "source_id": source_id,
            "slug": slug,
            "name": display_name,
            "enabled": enabled,
            "workspace_id": workspace_id,
            "write_key": write_key,
            "categories": categories,
            "description": meta_description,
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
