from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import AmplitudeAuthError, AmplitudeError, AmplitudeRateLimitError
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
    last_exc: AmplitudeError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except AmplitudeAuthError:
            raise
        except AmplitudeRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except AmplitudeError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_id(api_key: str, event_type: str, date: str) -> str:
    """Return SHA-256(api_key + ':' + event_type + ':' + date)[:16].

    Stable time-series document identifier for deduplication across syncs.
    """
    raw = f"{api_key}:{event_type}:{date}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _stable_id_simple(raw_id: str) -> str:
    """Return SHA-256(raw_id)[:16] — for cohort / taxonomy IDs."""
    return hashlib.sha256(raw_id.encode()).hexdigest()[:16]


def normalize_event_data(
    event_type: str,
    series_data: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    api_key: str,
) -> list[ConnectorDocument]:
    """Convert Amplitude event segmentation response into ConnectorDocuments.

    Amplitude returns:
    {
        "data": {
            "series": [[count, ...], ...],
            "xValues": ["2024-01-01", ...],
            ...
        }
    }

    Each date becomes one ConnectorDocument with count as content.
    """
    data = series_data.get("data", series_data)
    x_values: list[str] = data.get("xValues", [])
    series: list[list[Any]] = data.get("series", [])

    if not series or not x_values:
        return []

    counts: list[Any] = series[0] if series else []
    documents: list[ConnectorDocument] = []

    for i, date in enumerate(x_values):
        count = counts[i] if i < len(counts) else 0
        stable = _stable_id(api_key, event_type, date)
        content_parts = [
            f"Event type: {event_type}",
            f"Date: {date}",
            f"Count: {count}",
        ]
        doc = ConnectorDocument(
            source_id=stable,
            title=f"Amplitude event: {event_type} on {date}",
            content="\n".join(content_parts),
            connector_id=connector_id,
            tenant_id=tenant_id,
            source_url=f"https://app.amplitude.com/analytics",
            metadata={
                "event_type": event_type,
                "date": date,
                "count": count,
            },
        )
        documents.append(doc)

    return documents


def normalize_cohort(
    cohort: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert an Amplitude cohort object into a ConnectorDocument."""
    cohort_id: str = str(cohort.get("id", cohort.get("cohortId", "")))
    name: str = cohort.get("name", "Unnamed Cohort")
    size: int = cohort.get("size", 0)
    description: str = cohort.get("description", "")
    last_computed: str = str(cohort.get("lastComputed", cohort.get("last_computed", "")))

    content_parts = [
        f"Cohort ID: {cohort_id}",
        f"Name: {name}",
        f"Size: {size}",
    ]
    if description:
        content_parts.append(f"Description: {description}")
    if last_computed:
        content_parts.append(f"Last computed: {last_computed}")

    return ConnectorDocument(
        source_id=_stable_id_simple(cohort_id) if cohort_id else cohort_id,
        title=f"Amplitude cohort: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://app.amplitude.com/analytics/cohorts",
        metadata={
            "cohort_id": cohort_id,
            "name": name,
            "size": size,
            "description": description,
            "last_computed": last_computed,
        },
    )


def normalize_event_type(event: dict[str, Any]) -> ConnectorDocument:
    """Convert an Amplitude taxonomy event-type dict into a ConnectorDocument.

    ID = sha256("event_type:" + event.get("value", ""))[:16]

    Args:
        event: Raw event-type dict from GET /taxonomy/event.

    Returns:
        ConnectorDocument with source="amplitude", type="event_type".
    """
    value: str = event.get("value", event.get("name", ""))
    raw_id = f"event_type:{value}"
    source_id = hashlib.sha256(raw_id.encode()).hexdigest()[:16]

    display_name: str = event.get("displayName", event.get("display_name", value))
    category: str = event.get("category", event.get("categoryName", ""))
    description: str = event.get("description", "")

    content_parts = [f"Event type: {value}"]
    if display_name and display_name != value:
        content_parts.append(f"Display name: {display_name}")
    if category:
        content_parts.append(f"Category: {category}")
    if description:
        content_parts.append(f"Description: {description}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Amplitude event type: {display_name or value}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url="https://app.amplitude.com/analytics",
        metadata={
            "source": "amplitude",
            "type": "event_type",
            "value": value,
            "display_name": display_name,
            "category": category,
            "description": description,
        },
    )


def normalize_chart(chart: dict[str, Any]) -> ConnectorDocument:
    """Convert an Amplitude chart/dashboard dict into a ConnectorDocument.

    ID = sha256("chart:" + str(chart.get("id", "")))[:16]

    Args:
        chart: Raw chart dict from GET /chart/list.

    Returns:
        ConnectorDocument with source="amplitude", type="analytics_chart".
    """
    chart_id: str = str(chart.get("id", chart.get("chartId", "")))
    raw_id = f"chart:{chart_id}"
    source_id = hashlib.sha256(raw_id.encode()).hexdigest()[:16]

    title: str = chart.get("title", chart.get("name", "Untitled Chart"))
    chart_type: str = chart.get("type", chart.get("chart_type", ""))
    created_at: str = str(chart.get("createdAt", chart.get("created_at", "")))
    updated_at: str = str(chart.get("updatedAt", chart.get("updated_at", "")))

    content_parts = [f"Chart ID: {chart_id}", f"Title: {title}"]
    if chart_type:
        content_parts.append(f"Type: {chart_type}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Amplitude chart: {title}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url="https://app.amplitude.com/analytics",
        metadata={
            "source": "amplitude",
            "type": "analytics_chart",
            "chart_id": chart_id,
            "title": title,
            "chart_type": chart_type,
            "created_at": created_at,
            "updated_at": updated_at,
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
