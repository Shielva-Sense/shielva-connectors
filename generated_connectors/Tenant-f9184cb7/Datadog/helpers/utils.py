from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import DatadogAuthError, DatadogError, DatadogRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

DATADOG_APP_BASE = "https://app.datadoghq.com"


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
    last_exc: DatadogError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except DatadogAuthError:
            raise
        except DatadogRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except DatadogError as exc:
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


def normalize_monitor(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Datadog monitor object into a ConnectorDocument.

    Stable ID = SHA-256("monitor:" + str(id))[:16]
    """
    monitor_id: int = raw.get("id", 0)
    name: str = raw.get("name", "Unnamed Monitor")
    status: str = raw.get("overall_state", raw.get("status", "Unknown"))
    monitor_type: str = raw.get("type", "")
    query: str = raw.get("query", "")
    message: str = raw.get("message", "")
    tags: list[str] = raw.get("tags", [])
    created: str = str(raw.get("created", ""))
    modified: str = str(raw.get("modified", ""))

    source_id = _stable_id("monitor", str(monitor_id))
    content_parts = [
        f"Monitor ID: {monitor_id}",
        f"Name: {name}",
        f"Type: {monitor_type}",
        f"Status: {status}",
    ]
    if query:
        content_parts.append(f"Query: {query}")
    if message:
        content_parts.append(f"Message: {message}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    if created:
        content_parts.append(f"Created: {created}")
    if modified:
        content_parts.append(f"Modified: {modified}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Datadog monitor: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{DATADOG_APP_BASE}/monitors/{monitor_id}",
        metadata={
            "monitor_id": monitor_id,
            "name": name,
            "type": monitor_type,
            "status": status,
            "tags": tags,
            "created": created,
            "modified": modified,
        },
    )


def normalize_dashboard(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Datadog dashboard object into a ConnectorDocument.

    Stable ID = SHA-256("dashboard:" + id)[:16]
    """
    dashboard_id: str = str(raw.get("id", ""))
    title: str = raw.get("title", "Unnamed Dashboard")
    description: str = raw.get("description", "")
    layout_type: str = raw.get("layout_type", "")
    url: str = raw.get("url", "")
    created: str = str(raw.get("created_at", ""))
    modified: str = str(raw.get("modified_at", ""))
    author: dict[str, Any] = raw.get("author_handle", {}) if isinstance(raw.get("author_handle"), dict) else {}
    author_name: str = raw.get("author_handle", "") if isinstance(raw.get("author_handle"), str) else str(author)

    source_id = _stable_id("dashboard", dashboard_id)
    content_parts = [
        f"Dashboard ID: {dashboard_id}",
        f"Title: {title}",
        f"Layout: {layout_type}",
    ]
    if description:
        content_parts.append(f"Description: {description}")
    if author_name:
        content_parts.append(f"Author: {author_name}")
    if created:
        content_parts.append(f"Created: {created}")
    if modified:
        content_parts.append(f"Modified: {modified}")

    full_url = url if url else f"{DATADOG_APP_BASE}/dashboard/{dashboard_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=f"Datadog dashboard: {title}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=full_url,
        metadata={
            "dashboard_id": dashboard_id,
            "title": title,
            "layout_type": layout_type,
            "description": description,
            "author": author_name,
            "created_at": created,
            "modified_at": modified,
        },
    )


def normalize_host(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Datadog host object into a ConnectorDocument.

    Stable ID = SHA-256("host:" + host_name)[:16]
    """
    host_name: str = raw.get("host_name", raw.get("name", "unknown"))
    host_id: int = raw.get("id", 0)
    aliases: list[str] = raw.get("aliases", [])
    apps: list[str] = raw.get("apps", [])
    tags_by_source: dict[str, Any] = raw.get("tags_by_source", {})
    up: bool = raw.get("up", False)
    last_reported_time: int = raw.get("last_reported_time", 0)
    sources: list[str] = raw.get("sources", [])

    source_id = _stable_id("host", host_name)
    status_str = "up" if up else "down"
    content_parts = [
        f"Host: {host_name}",
        f"Status: {status_str}",
        f"Host ID: {host_id}",
    ]
    if aliases:
        content_parts.append(f"Aliases: {', '.join(aliases)}")
    if apps:
        content_parts.append(f"Apps: {', '.join(apps)}")
    if sources:
        content_parts.append(f"Sources: {', '.join(sources)}")
    if last_reported_time:
        content_parts.append(f"Last reported: {last_reported_time}")

    all_tags: list[str] = []
    for tag_list in tags_by_source.values():
        if isinstance(tag_list, list):
            all_tags.extend(tag_list)
    if all_tags:
        content_parts.append(f"Tags: {', '.join(all_tags[:20])}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Datadog host: {host_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{DATADOG_APP_BASE}/infrastructure?host={host_name}",
        metadata={
            "host_name": host_name,
            "host_id": host_id,
            "up": up,
            "aliases": aliases,
            "apps": apps,
            "sources": sources,
            "last_reported_time": last_reported_time,
        },
    )


def normalize_event(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Datadog event object into a ConnectorDocument.

    Stable ID = SHA-256("event:" + str(id))[:16]
    """
    event_id: int = raw.get("id", 0)
    title: str = raw.get("title", "Unnamed Event")
    text: str = raw.get("text", "")
    alert_type: str = raw.get("alert_type", "")
    date_happened: int = raw.get("date_happened", 0)
    host: str = raw.get("host", "")
    tags: list[str] = raw.get("tags", [])
    url: str = raw.get("url", "")
    source: str = raw.get("source", "")

    source_id = _stable_id("event", str(event_id))
    content_parts = [
        f"Event ID: {event_id}",
        f"Title: {title}",
        f"Alert type: {alert_type}",
    ]
    if text:
        content_parts.append(f"Text: {text}")
    if host:
        content_parts.append(f"Host: {host}")
    if source:
        content_parts.append(f"Source: {source}")
    if date_happened:
        content_parts.append(f"Date: {date_happened}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")

    event_url = url if url else f"{DATADOG_APP_BASE}/event/event?id={event_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=f"Datadog event: {title}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=event_url,
        metadata={
            "event_id": event_id,
            "title": title,
            "alert_type": alert_type,
            "date_happened": date_happened,
            "host": host,
            "source": source,
            "tags": tags,
        },
    )
