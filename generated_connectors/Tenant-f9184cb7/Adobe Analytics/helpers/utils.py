"""Utility helpers for the Adobe Analytics connector.

Includes:
- with_retry: exponential backoff, skip auth errors
- normalize_report_suite / normalize_segment / normalize_calculated_metric
"""
from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import (
    AdobeAnalyticsAuthError,
    AdobeAnalyticsError,
    AdobeAnalyticsRateLimitError,
)
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

ADOBE_ANALYTICS_BASE_URL = "https://analytics.adobe.io"


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
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: AdobeAnalyticsError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except AdobeAnalyticsAuthError:
            raise
        except AdobeAnalyticsRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except AdobeAnalyticsError as exc:
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


def _stable_id(prefix: str, raw_value: str) -> str:
    """Return SHA-256(prefix + ':' + raw_value)[:16] as a stable document ID."""
    raw = f"{prefix}:{raw_value}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_report_suite(
    rs: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert an Adobe Analytics report suite object into a ConnectorDocument.

    Stable ID: SHA-256('report_suite:' + rs['rsid'])[:16]
    """
    rsid: str = str(rs.get("rsid", ""))
    name: str = rs.get("name", rs.get("rsName", "Unnamed Report Suite"))
    currency: str = rs.get("currency", "")
    timezone: str = rs.get("timezone", rs.get("timezoneZoneinfo", ""))
    status: str = rs.get("status", "")

    content_parts = [
        f"Report Suite ID: {rsid}",
        f"Name: {name}",
    ]
    if currency:
        content_parts.append(f"Currency: {currency}")
    if timezone:
        content_parts.append(f"Timezone: {timezone}")
    if status:
        content_parts.append(f"Status: {status}")

    return ConnectorDocument(
        source_id=_stable_id("report_suite", rsid),
        title=f"Adobe Analytics report suite: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://analytics.adobe.com",
        metadata={
            "rsid": rsid,
            "name": name,
            "currency": currency,
            "timezone": timezone,
            "status": status,
            "type": "report_suite",
        },
    )


def normalize_segment(
    s: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert an Adobe Analytics segment object into a ConnectorDocument.

    Stable ID: SHA-256('segment:' + s['id'])[:16]
    """
    seg_id: str = str(s.get("id", ""))
    name: str = s.get("name", "Unnamed Segment")
    description: str = s.get("description", "")
    owner: str = ""
    owner_obj = s.get("owner")
    if isinstance(owner_obj, dict):
        owner = owner_obj.get("name", "")
    elif isinstance(owner_obj, str):
        owner = owner_obj
    tags: list[str] = [t.get("name", "") for t in s.get("tags", []) if isinstance(t, dict)]

    content_parts = [
        f"Segment ID: {seg_id}",
        f"Name: {name}",
    ]
    if description:
        content_parts.append(f"Description: {description}")
    if owner:
        content_parts.append(f"Owner: {owner}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")

    return ConnectorDocument(
        source_id=_stable_id("segment", seg_id),
        title=f"Adobe Analytics segment: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://analytics.adobe.com",
        metadata={
            "id": seg_id,
            "name": name,
            "description": description,
            "owner": owner,
            "tags": tags,
            "type": "segment",
        },
    )


def normalize_calculated_metric(
    m: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert an Adobe Analytics calculated metric into a ConnectorDocument.

    Stable ID: SHA-256('calculated_metric:' + m['id'])[:16]
    """
    metric_id: str = str(m.get("id", ""))
    name: str = m.get("name", "Unnamed Calculated Metric")
    description: str = m.get("description", "")
    formula: str = m.get("formula", "")
    owner: str = ""
    owner_obj = m.get("owner")
    if isinstance(owner_obj, dict):
        owner = owner_obj.get("name", "")
    elif isinstance(owner_obj, str):
        owner = owner_obj
    polarity: str = m.get("polarity", "")
    precision: int = m.get("precision", 0)

    content_parts = [
        f"Calculated Metric ID: {metric_id}",
        f"Name: {name}",
    ]
    if description:
        content_parts.append(f"Description: {description}")
    if formula:
        content_parts.append(f"Formula: {formula}")
    if owner:
        content_parts.append(f"Owner: {owner}")
    if polarity:
        content_parts.append(f"Polarity: {polarity}")
    if precision:
        content_parts.append(f"Precision: {precision}")

    return ConnectorDocument(
        source_id=_stable_id("calculated_metric", metric_id),
        title=f"Adobe Analytics calculated metric: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://analytics.adobe.com",
        metadata={
            "id": metric_id,
            "name": name,
            "description": description,
            "formula": formula,
            "owner": owner,
            "polarity": polarity,
            "precision": precision,
            "type": "calculated_metric",
        },
    )
