from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import LookerAuthError, LookerError, LookerRateLimitError
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
    last_exc: LookerError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except LookerAuthError:
            raise
        except LookerRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except LookerError as exc:
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


def normalize_look(
    l: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    base_url: str = "",
) -> ConnectorDocument:
    """Normalize a Looker Look object to a ConnectorDocument."""
    raw_id = str(l.get("id", ""))
    stable_id = _stable_id("look", raw_id)
    title = l.get("title", "") or l.get("name", "")
    description = l.get("description", "") or ""
    model_name = ""
    query = l.get("query", {})
    if isinstance(query, dict):
        model_name = query.get("model", "") or ""
    folder = l.get("folder", {})
    folder_name = folder.get("name", "") if isinstance(folder, dict) else ""
    space = l.get("space", {})
    space_name = space.get("name", "") if isinstance(space, dict) else ""
    created_at = l.get("created_at", "") or ""
    updated_at = l.get("updated_at", "") or ""
    user_id = str(l.get("user_id", "")) if l.get("user_id") is not None else ""

    source_url = ""
    if base_url and raw_id:
        source_url = f"{base_url.rstrip('/')}/looks/{raw_id}"

    content_parts = [f"Look: {title}"]
    if description:
        content_parts.append(f"Description: {description}")
    if model_name:
        content_parts.append(f"Model: {model_name}")
    if folder_name:
        content_parts.append(f"Folder: {folder_name}")
    elif space_name:
        content_parts.append(f"Space: {space_name}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    return ConnectorDocument(
        source_id=stable_id,
        title=title or f"Look {raw_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "type": "look",
            "looker_id": raw_id,
            "model_name": model_name,
            "folder_name": folder_name or space_name,
            "description": description,
            "user_id": user_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_dashboard(
    d: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    base_url: str = "",
) -> ConnectorDocument:
    """Normalize a Looker Dashboard object to a ConnectorDocument."""
    raw_id = str(d.get("id", ""))
    stable_id = _stable_id("dashboard", raw_id)
    title = d.get("title", "") or d.get("name", "")
    description = d.get("description", "") or ""
    folder = d.get("folder", {})
    folder_name = folder.get("name", "") if isinstance(folder, dict) else ""
    space = d.get("space", {})
    space_name = space.get("name", "") if isinstance(space, dict) else ""
    created_at = d.get("created_at", "") or ""
    updated_at = d.get("updated_at", "") or ""
    user_id = str(d.get("user_id", "")) if d.get("user_id") is not None else ""

    source_url = ""
    if base_url and raw_id:
        source_url = f"{base_url.rstrip('/')}/dashboards/{raw_id}"

    content_parts = [f"Dashboard: {title}"]
    if description:
        content_parts.append(f"Description: {description}")
    if folder_name:
        content_parts.append(f"Folder: {folder_name}")
    elif space_name:
        content_parts.append(f"Space: {space_name}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    return ConnectorDocument(
        source_id=stable_id,
        title=title or f"Dashboard {raw_id}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "type": "dashboard",
            "looker_id": raw_id,
            "folder_name": folder_name or space_name,
            "description": description,
            "user_id": user_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_model(
    m: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    base_url: str = "",
) -> ConnectorDocument:
    """Normalize a Looker LookML Model object to a ConnectorDocument."""
    name = m.get("name", "") or ""
    stable_id = _stable_id("model", name)
    label = m.get("label", "") or ""
    title = label or name
    project_name = m.get("project_name", "") or ""
    allowed_db_connection_names = m.get("allowed_db_connection_names", [])
    explore_count = 0
    explores = m.get("explores", [])
    if isinstance(explores, list):
        explore_count = len(explores)

    source_url = ""
    if base_url and name:
        source_url = f"{base_url.rstrip('/')}/explore/{name}"

    content_parts = [f"LookML Model: {title}"]
    if project_name:
        content_parts.append(f"Project: {project_name}")
    if explore_count:
        content_parts.append(f"Explores: {explore_count}")
    if allowed_db_connection_names:
        content_parts.append(f"Connections: {', '.join(str(c) for c in allowed_db_connection_names)}")

    return ConnectorDocument(
        source_id=stable_id,
        title=title or f"Model {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "type": "lookml_model",
            "name": name,
            "label": label,
            "project_name": project_name,
            "explore_count": explore_count,
        },
    )
