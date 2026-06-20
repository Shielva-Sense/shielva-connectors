"""Utility helpers for the Google Analytics 4 connector.

Includes:
- with_retry: exponential backoff, skips auth errors
- normalize_report_row: GA4 runReport row → ConnectorDocument
- normalize_property: GA4 property → ConnectorDocument
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

# Allow running directly from connector root
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from exceptions import (
    GoogleAnalyticsAuthError,
    GoogleAnalyticsError,
    GoogleAnalyticsRateLimitError,
)
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
    Rate-limit errors honour the Retry-After value when present.

    Args:
        fn: Async callable to invoke.
        *args: Positional arguments forwarded to fn.
        max_attempts: Maximum number of attempts (default 3).
        base_delay: Base delay in seconds for exponential backoff (default 1.0).
        max_delay: Maximum delay cap in seconds (default 30.0).
        **kwargs: Keyword arguments forwarded to fn.

    Returns:
        Return value of fn on success.

    Raises:
        GoogleAnalyticsAuthError: Immediately, without retry.
        GoogleAnalyticsError: After max_attempts are exhausted.
    """
    last_exc: GoogleAnalyticsError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except GoogleAnalyticsAuthError:
            raise
        except GoogleAnalyticsRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except GoogleAnalyticsError as exc:
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


def normalize_report_row(
    row: dict[str, Any],
    property_id: str,
    report_date: str,
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a GA4 runReport row into a ConnectorDocument.

    Stable ID: SHA-256('ga_row:' + property_id + '_' + report_date + '_' + str(hash(str(row))))[:16]

    Args:
        row: A single row dict from GA4 runReport response (contains dimensionValues + metricValues).
        property_id: GA4 property ID string (e.g. "123456789").
        report_date: Date string for the report (e.g. "2024-01-15").
        connector_id: Shielva connector ID for provenance.
        tenant_id: Shielva tenant ID for data isolation.

    Returns:
        ConnectorDocument with stable source_id and structured metadata.
    """
    # Build stable ID from property + date + row hash
    row_hash = str(hash(str(row)))
    raw_key = f"ga_row:{property_id}_{report_date}_{row_hash}"
    doc_id = hashlib.sha256(raw_key.encode()).hexdigest()[:16]

    # Extract dimension values
    dimension_values: list[dict[str, Any]] = row.get("dimensionValues", [])
    metric_values: list[dict[str, Any]] = row.get("metricValues", [])

    dim_strs = [str(dv.get("value", "")) for dv in dimension_values]
    metric_strs = [str(mv.get("value", "")) for mv in metric_values]

    content_parts = [
        f"Property ID: {property_id}",
        f"Report Date: {report_date}",
    ]
    if dim_strs:
        content_parts.append(f"Dimensions: {', '.join(dim_strs)}")
    if metric_strs:
        content_parts.append(f"Metrics: {', '.join(metric_strs)}")

    return ConnectorDocument(
        source_id=doc_id,
        title=f"GA4 Analytics Row: {property_id} / {report_date}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://analytics.google.com/analytics/web/#/p{property_id}",
        metadata={
            "property_id": property_id,
            "report_date": report_date,
            "dimension_values": dimension_values,
            "metric_values": metric_values,
            "type": "analytics_report_row",
            "source": "google_analytics",
        },
    )


def normalize_property(
    prop: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a GA4 property dict into a ConnectorDocument.

    Stable ID: SHA-256('property:' + prop['name'])[:16]

    Args:
        prop: GA4 property dict from Admin API (contains 'name', 'displayName', etc.).
        connector_id: Shielva connector ID for provenance.
        tenant_id: Shielva tenant ID for data isolation.

    Returns:
        ConnectorDocument with stable source_id and structured metadata.
    """
    prop_name: str = prop.get("name", "")
    display_name: str = prop.get("displayName", "Unnamed Property")
    industry_category: str = prop.get("industryCategory", "")
    time_zone: str = prop.get("timeZone", "")
    currency_code: str = prop.get("currencyCode", "")
    create_time: str = prop.get("createTime", "")
    parent: str = prop.get("parent", "")

    doc_id = _stable_id("property", prop_name)

    content_parts = [
        f"Property: {prop_name}",
        f"Display Name: {display_name}",
    ]
    if industry_category:
        content_parts.append(f"Industry: {industry_category}")
    if time_zone:
        content_parts.append(f"Timezone: {time_zone}")
    if currency_code:
        content_parts.append(f"Currency: {currency_code}")
    if parent:
        content_parts.append(f"Account: {parent}")
    if create_time:
        content_parts.append(f"Created: {create_time}")

    return ConnectorDocument(
        source_id=doc_id,
        title=f"GA4 Property: {display_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://analytics.google.com",
        metadata={
            "name": prop_name,
            "display_name": display_name,
            "industry_category": industry_category,
            "time_zone": time_zone,
            "currency_code": currency_code,
            "create_time": create_time,
            "parent": parent,
            "type": "ga4_property",
            "source": "google_analytics",
        },
    )
