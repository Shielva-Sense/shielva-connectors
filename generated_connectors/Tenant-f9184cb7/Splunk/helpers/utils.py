from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import SplunkAuthError, SplunkError, SplunkRateLimitError
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
    Rate-limit errors honour the retry_after value when present.
    """
    last_exc: SplunkError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except SplunkAuthError:
            raise
        except SplunkRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except SplunkError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_id(prefix: str, resource_id: str) -> str:
    """Return SHA-256(prefix + ':' + resource_id)[:16].

    Provides a stable, compact document identifier for deduplication across syncs.
    """
    raw = f"{prefix}:{resource_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_saved_search(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Splunk saved search entry into a ConnectorDocument.

    Stable ID = SHA-256("saved_search:" + name)[:16]
    """
    name: str = raw.get("name", "Unnamed Saved Search")
    content_raw: dict[str, Any] = raw.get("content", {}) if isinstance(raw.get("content"), dict) else {}
    search_query: str = content_raw.get("search", "")
    description: str = content_raw.get("description", "")
    cron_schedule: str = content_raw.get("cron_schedule", "")
    is_scheduled: bool = content_raw.get("is_scheduled", False)
    dispatch_earliest: str = str(content_raw.get("dispatch.earliest_time", ""))
    dispatch_latest: str = str(content_raw.get("dispatch.latest_time", ""))
    author: str = raw.get("author", "")
    acl: dict[str, Any] = raw.get("acl", {}) if isinstance(raw.get("acl"), dict) else {}
    app: str = acl.get("app", "")
    owner: str = acl.get("owner", author)

    source_id = _stable_id("saved_search", name)
    content_parts = [f"Saved Search: {name}"]
    if search_query:
        content_parts.append(f"Query: {search_query}")
    if description:
        content_parts.append(f"Description: {description}")
    if is_scheduled and cron_schedule:
        content_parts.append(f"Schedule: {cron_schedule}")
    if dispatch_earliest or dispatch_latest:
        content_parts.append(f"Time range: {dispatch_earliest} to {dispatch_latest}")
    if app:
        content_parts.append(f"App: {app}")
    if owner:
        content_parts.append(f"Owner: {owner}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Splunk saved search: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "name": name,
            "type": "saved_search",
            "search": search_query,
            "description": description,
            "is_scheduled": is_scheduled,
            "cron_schedule": cron_schedule,
            "app": app,
            "owner": owner,
        },
    )


def normalize_index(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Splunk index entry into a ConnectorDocument.

    Stable ID = SHA-256("index:" + name)[:16]
    """
    name: str = raw.get("name", "Unnamed Index")
    content_raw: dict[str, Any] = raw.get("content", {}) if isinstance(raw.get("content"), dict) else {}
    total_event_count: int = int(content_raw.get("totalEventCount", 0))
    current_db_size_mb: int = int(content_raw.get("currentDBSizeMB", 0))
    max_total_data_size_mb: int = int(content_raw.get("maxTotalDataSizeMB", 0))
    index_type: str = content_raw.get("datatype", "event")
    home_path: str = content_raw.get("homePath", "")
    cold_path: str = content_raw.get("coldPath", "")
    frozen_time_period_in_secs: int = int(content_raw.get("frozenTimePeriodInSecs", 0))
    disabled: bool = content_raw.get("disabled", False)

    source_id = _stable_id("index", name)
    content_parts = [f"Index: {name}", f"Type: {index_type}"]
    if total_event_count:
        content_parts.append(f"Total events: {total_event_count:,}")
    if current_db_size_mb:
        content_parts.append(f"Current DB size: {current_db_size_mb} MB")
    if max_total_data_size_mb:
        content_parts.append(f"Max size: {max_total_data_size_mb} MB")
    if frozen_time_period_in_secs:
        days = frozen_time_period_in_secs // 86400
        content_parts.append(f"Retention: {days} days")
    if home_path:
        content_parts.append(f"Home path: {home_path}")
    status = "disabled" if disabled else "enabled"
    content_parts.append(f"Status: {status}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Splunk index: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "name": name,
            "type": "index",
            "datatype": index_type,
            "totalEventCount": total_event_count,
            "currentDBSizeMB": current_db_size_mb,
            "maxTotalDataSizeMB": max_total_data_size_mb,
            "disabled": disabled,
            "frozenTimePeriodInSecs": frozen_time_period_in_secs,
            "homePath": home_path,
            "coldPath": cold_path,
        },
    )


def normalize_app(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Splunk app entry into a ConnectorDocument.

    Stable ID = SHA-256("app:" + name)[:16]
    """
    name: str = raw.get("name", "Unnamed App")
    content_raw: dict[str, Any] = raw.get("content", {}) if isinstance(raw.get("content"), dict) else {}
    label: str = content_raw.get("label", name)
    version: str = content_raw.get("version", "")
    description: str = content_raw.get("description", "")
    author: str = content_raw.get("author", "")
    disabled: bool = content_raw.get("disabled", False)
    configured: bool = content_raw.get("configured", False)
    visible: bool = content_raw.get("visible", True)

    source_id = _stable_id("app", name)
    content_parts = [f"App: {label} ({name})"]
    if version:
        content_parts.append(f"Version: {version}")
    if description:
        content_parts.append(f"Description: {description}")
    if author:
        content_parts.append(f"Author: {author}")
    status = "disabled" if disabled else "enabled"
    content_parts.append(f"Status: {status}")
    content_parts.append(f"Configured: {configured}")
    content_parts.append(f"Visible: {visible}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Splunk app: {label}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "name": name,
            "type": "app",
            "label": label,
            "version": version,
            "description": description,
            "author": author,
            "disabled": disabled,
            "configured": configured,
            "visible": visible,
        },
    )
