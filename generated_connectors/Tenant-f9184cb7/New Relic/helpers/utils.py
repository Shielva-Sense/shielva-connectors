from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import NewRelicAuthError, NewRelicError, NewRelicRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

NEWRELIC_APP_BASE_US = "https://one.newrelic.com"
NEWRELIC_APP_BASE_EU = "https://one.eu.newrelic.com"


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
    last_exc: NewRelicError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except NewRelicAuthError:
            raise
        except NewRelicRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except NewRelicError as exc:
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


def normalize_alert(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a New Relic alert policy object into a ConnectorDocument.

    Stable ID = SHA-256("alert:" + str(id))[:16]
    """
    alert_id: int = raw.get("id", 0)
    name: str = raw.get("name", "Unnamed Alert Policy")
    incident_preference: str = raw.get("incident_preference", "")
    created_at: str = str(raw.get("created_at", ""))
    updated_at: str = str(raw.get("updated_at", ""))

    source_id = _stable_id("alert", str(alert_id))
    content_parts = [
        f"Alert Policy ID: {alert_id}",
        f"Name: {name}",
    ]
    if incident_preference:
        content_parts.append(f"Incident preference: {incident_preference}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"New Relic alert policy: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{NEWRELIC_APP_BASE_US}/alerts-ai/policies/{alert_id}",
        metadata={
            "alert_id": alert_id,
            "name": name,
            "incident_preference": incident_preference,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_application(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a New Relic APM application object into a ConnectorDocument.

    Stable ID = SHA-256("application:" + str(id))[:16]
    """
    app_id: int = raw.get("id", 0)
    name: str = raw.get("name", "Unnamed Application")
    language: str = raw.get("language", "")
    health_status: str = raw.get("health_status", "unknown")
    reporting: bool = raw.get("reporting", False)
    last_reported_at: str = str(raw.get("last_reported_at", ""))

    # Summary nested object
    summary: dict[str, Any] = raw.get("application_summary", {}) or {}
    response_time: float = summary.get("response_time", 0.0)
    throughput: float = summary.get("throughput", 0.0)
    error_rate: float = summary.get("error_rate", 0.0)
    apdex_score: float = summary.get("apdex_score", 0.0)

    source_id = _stable_id("application", str(app_id))
    content_parts = [
        f"Application ID: {app_id}",
        f"Name: {name}",
        f"Health: {health_status}",
        f"Reporting: {reporting}",
    ]
    if language:
        content_parts.append(f"Language: {language}")
    if response_time:
        content_parts.append(f"Response time: {response_time}ms")
    if throughput:
        content_parts.append(f"Throughput: {throughput} rpm")
    if error_rate:
        content_parts.append(f"Error rate: {error_rate}%")
    if apdex_score:
        content_parts.append(f"Apdex score: {apdex_score}")
    if last_reported_at:
        content_parts.append(f"Last reported: {last_reported_at}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"New Relic application: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{NEWRELIC_APP_BASE_US}/apm/applications/{app_id}",
        metadata={
            "app_id": app_id,
            "name": name,
            "language": language,
            "health_status": health_status,
            "reporting": reporting,
            "response_time": response_time,
            "throughput": throughput,
            "error_rate": error_rate,
            "apdex_score": apdex_score,
            "last_reported_at": last_reported_at,
        },
    )


def normalize_incident(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a New Relic alert incident object into a ConnectorDocument.

    Handles both NerdGraph shape (incidentId) and REST shape (id).
    Stable ID = SHA-256("incident:" + str(id))[:16]
    """
    incident_id: str = str(raw.get("incidentId", raw.get("id", "0")))
    title: str = raw.get("title", raw.get("name", "Unnamed Incident"))
    state: str = raw.get("state", raw.get("status", "unknown"))
    priority: str = raw.get("priority", raw.get("severity", ""))
    created_at: str = str(raw.get("createdAt", raw.get("opened_at", "")))
    closed_at: str = str(raw.get("closedAt", raw.get("closed_at", "")))
    duration: Any = raw.get("duration", "")

    source_id = _stable_id("incident", incident_id)
    content_parts = [
        f"Incident ID: {incident_id}",
        f"Title: {title}",
        f"State: {state}",
    ]
    if priority:
        content_parts.append(f"Priority: {priority}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if closed_at and closed_at != "None":
        content_parts.append(f"Closed: {closed_at}")
    if duration:
        content_parts.append(f"Duration: {duration}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"New Relic incident: {title}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{NEWRELIC_APP_BASE_US}/alerts-ai/incidents/{incident_id}",
        metadata={
            "incident_id": incident_id,
            "title": title,
            "state": state,
            "priority": priority,
            "created_at": created_at,
            "closed_at": closed_at,
            "duration": duration,
        },
    )


def normalize_dashboard(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a New Relic dashboard entity object into a ConnectorDocument.

    Handles NerdGraph entity shape (guid, name, createdAt, updatedAt).
    Stable ID = SHA-256("dashboard:" + guid)[:16]
    """
    guid: str = str(raw.get("guid", raw.get("id", "")))
    name: str = raw.get("name", "Unnamed Dashboard")
    account_id: Any = raw.get("accountId", "")
    created_at: str = str(raw.get("createdAt", ""))
    updated_at: str = str(raw.get("updatedAt", ""))
    permissions: str = raw.get("permissions", "")

    source_id = _stable_id("dashboard", guid)
    content_parts = [
        f"Dashboard GUID: {guid}",
        f"Name: {name}",
    ]
    if account_id:
        content_parts.append(f"Account ID: {account_id}")
    if permissions:
        content_parts.append(f"Permissions: {permissions}")
    if created_at and created_at != "None":
        content_parts.append(f"Created: {created_at}")
    if updated_at and updated_at != "None":
        content_parts.append(f"Updated: {updated_at}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"New Relic dashboard: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{NEWRELIC_APP_BASE_US}/dashboards/{guid}",
        metadata={
            "guid": guid,
            "name": name,
            "account_id": account_id,
            "permissions": permissions,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
