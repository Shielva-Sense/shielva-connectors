from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import TableauAuthError, TableauError, TableauRateLimitError
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
    last_exc: TableauError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except TableauAuthError:
            raise
        except TableauRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except TableauError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_id(prefix: str, raw_id: str) -> str:
    """Return a 16-char hex digest: sha256(prefix + ":" + raw_id)[:16]."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


def normalize_workbook(
    wb: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    server_url: str = "",
) -> ConnectorDocument:
    """Normalize a Tableau workbook object to a ConnectorDocument."""
    raw_id = wb.get("id", "")
    stable_id = _stable_id("workbook", raw_id)
    name = wb.get("name", "")
    description = wb.get("description", "")
    project = wb.get("project", {})
    project_name = project.get("name", "") if isinstance(project, dict) else ""
    owner = wb.get("owner", {})
    owner_name = owner.get("name", "") if isinstance(owner, dict) else ""
    created_at = wb.get("createdAt", "")
    updated_at = wb.get("updatedAt", "")
    content_url = wb.get("contentUrl", "")

    source_url = ""
    if server_url and content_url:
        source_url = f"{server_url.rstrip('/')}/#/views/{content_url}"

    content_parts = [f"Workbook: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    if project_name:
        content_parts.append(f"Project: {project_name}")
    if owner_name:
        content_parts.append(f"Owner: {owner_name}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    return ConnectorDocument(
        source_id=stable_id,
        title=name or f"Workbook {raw_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "type": "workbook",
            "tableau_id": raw_id,
            "project_name": project_name,
            "owner_name": owner_name,
            "content_url": content_url,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_view(
    v: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    server_url: str = "",
) -> ConnectorDocument:
    """Normalize a Tableau view object to a ConnectorDocument."""
    raw_id = v.get("id", "")
    stable_id = _stable_id("view", raw_id)
    name = v.get("name", "")
    content_url = v.get("contentUrl", "")
    workbook = v.get("workbook", {})
    workbook_id = workbook.get("id", "") if isinstance(workbook, dict) else ""
    owner = v.get("owner", {})
    owner_name = owner.get("name", "") if isinstance(owner, dict) else ""
    created_at = v.get("createdAt", "")
    updated_at = v.get("updatedAt", "")

    source_url = ""
    if server_url and content_url:
        source_url = f"{server_url.rstrip('/')}/#/views/{content_url}"

    content_parts = [f"View: {name}"]
    if workbook_id:
        content_parts.append(f"Workbook ID: {workbook_id}")
    if owner_name:
        content_parts.append(f"Owner: {owner_name}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    return ConnectorDocument(
        source_id=stable_id,
        title=name or f"View {raw_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "type": "view",
            "tableau_id": raw_id,
            "workbook_id": workbook_id,
            "owner_name": owner_name,
            "content_url": content_url,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_datasource(
    ds: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    server_url: str = "",
) -> ConnectorDocument:
    """Normalize a Tableau datasource object to a ConnectorDocument."""
    raw_id = ds.get("id", "")
    stable_id = _stable_id("datasource", raw_id)
    name = ds.get("name", "")
    description = ds.get("description", "")
    content_url = ds.get("contentUrl", "")
    ds_type = ds.get("type", "")
    project = ds.get("project", {})
    project_name = project.get("name", "") if isinstance(project, dict) else ""
    owner = ds.get("owner", {})
    owner_name = owner.get("name", "") if isinstance(owner, dict) else ""
    created_at = ds.get("createdAt", "")
    updated_at = ds.get("updatedAt", "")

    source_url = ""
    if server_url and content_url:
        source_url = f"{server_url.rstrip('/')}/#/datasources/{content_url}"

    content_parts = [f"Datasource: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    if ds_type:
        content_parts.append(f"Type: {ds_type}")
    if project_name:
        content_parts.append(f"Project: {project_name}")
    if owner_name:
        content_parts.append(f"Owner: {owner_name}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    return ConnectorDocument(
        source_id=stable_id,
        title=name or f"Datasource {raw_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "type": "datasource",
            "tableau_id": raw_id,
            "datasource_type": ds_type,
            "project_name": project_name,
            "owner_name": owner_name,
            "content_url": content_url,
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
