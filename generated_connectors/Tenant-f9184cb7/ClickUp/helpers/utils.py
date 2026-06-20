"""ClickUp connector — normalization helpers and retry utility."""
from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, Dict, List, TypeVar

from exceptions import ClickUpAuthError, ClickUpError, ClickUpRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BASE_DELAY_S: float = 1.0
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


# ── ID helpers ────────────────────────────────────────────────────────────────


def _stable_id(prefix: str, raw_id: str) -> str:
    """Return a 16-char stable document ID: sha256('<prefix>:<raw_id>')[:16]."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_task(
    task: Dict[str, Any],
    list_id: str = "",
    space_id: str = "",
) -> ConnectorDocument:
    """Convert a raw ClickUp task dict into a ConnectorDocument.

    Stable id = sha256("task:" + task_id)[:16].
    source = "clickup", type = "task".
    """
    task_id: str = str(task.get("id", ""))
    stable = _stable_id("task", task_id)

    name: str = task.get("name", "") or "Untitled Task"
    description: str = task.get("description", "") or ""
    url: str = task.get("url", "") or ""
    date_created: str = task.get("date_created", "") or ""
    date_updated: str = task.get("date_updated", "") or ""

    status_obj = task.get("status", {})
    status: str = (
        status_obj.get("status", "") if isinstance(status_obj, dict) else str(status_obj or "")
    )

    priority_obj = task.get("priority", {})
    priority: str = (
        priority_obj.get("priority", "") if isinstance(priority_obj, dict) else str(priority_obj or "")
    )

    list_obj = task.get("list", {})
    resolved_list_id: str = (
        list_obj.get("id", "") if isinstance(list_obj, dict) else ""
    ) or list_id
    list_name: str = list_obj.get("name", "") if isinstance(list_obj, dict) else ""

    space_obj = task.get("space", {})
    resolved_space_id: str = (
        space_obj.get("id", "") if isinstance(space_obj, dict) else ""
    ) or space_id

    folder_obj = task.get("folder", {})
    folder_name: str = folder_obj.get("name", "") if isinstance(folder_obj, dict) else ""

    assignees: List[str] = [
        a.get("username", "") for a in task.get("assignees", []) if isinstance(a, dict)
    ]
    tags: List[str] = [
        t.get("name", "") for t in task.get("tags", []) if isinstance(t, dict)
    ]

    content_parts: List[str] = [f"Task: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    if status:
        content_parts.append(f"Status: {status}")
    if priority:
        content_parts.append(f"Priority: {priority}")
    if list_name:
        content_parts.append(f"List: {list_name}")
    if folder_name:
        content_parts.append(f"Folder: {folder_name}")
    if assignees:
        content_parts.append(f"Assignees: {', '.join(a for a in assignees if a)}")
    if tags:
        content_parts.append(f"Tags: {', '.join(t for t in tags if t)}")
    if url:
        content_parts.append(f"URL: {url}")
    if date_created:
        content_parts.append(f"Created: {date_created}")
    if date_updated:
        content_parts.append(f"Updated: {date_updated}")

    metadata: Dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "priority": priority,
        "list_id": resolved_list_id,
        "list_name": list_name,
        "space_id": resolved_space_id,
        "folder_name": folder_name,
        "url": url,
        "date_created": date_created,
        "date_updated": date_updated,
        "assignees": assignees,
        "tags": tags,
    }

    return ConnectorDocument(
        id=stable,
        source="clickup",
        type="task",
        title=name,
        content="\n".join(content_parts),
        source_url=url,
        metadata=metadata,
    )


def normalize_list(
    list_obj: Dict[str, Any],
    space_id: str = "",
) -> ConnectorDocument:
    """Convert a raw ClickUp list dict into a ConnectorDocument.

    Stable id = sha256("list:" + list_id)[:16].
    source = "clickup", type = "task_list".
    """
    list_id: str = str(list_obj.get("id", ""))
    stable = _stable_id("list", list_id)

    name: str = list_obj.get("name", "") or "Untitled List"
    archived: bool = list_obj.get("archived", False) or False
    task_count: int = list_obj.get("task_count", 0) or 0
    due_date: str = list_obj.get("due_date", "") or ""

    folder_raw = list_obj.get("folder", {})
    folder_name: str = folder_raw.get("name", "") if isinstance(folder_raw, dict) else ""

    space_raw = list_obj.get("space", {})
    space_name: str = space_raw.get("name", "") if isinstance(space_raw, dict) else ""
    resolved_space_id: str = (
        space_raw.get("id", "") if isinstance(space_raw, dict) else ""
    ) or space_id

    status_raw = list_obj.get("status", {})
    status_name: str = status_raw.get("status", "") if isinstance(status_raw, dict) else ""

    content_parts: List[str] = [f"List: {name}"]
    if folder_name:
        content_parts.append(f"Folder: {folder_name}")
    if space_name:
        content_parts.append(f"Space: {space_name}")
    if status_name:
        content_parts.append(f"Status: {status_name}")
    if task_count:
        content_parts.append(f"Task count: {task_count}")
    if due_date:
        content_parts.append(f"Due date: {due_date}")
    if archived:
        content_parts.append("Archived: true")

    metadata: Dict[str, Any] = {
        "list_id": list_id,
        "folder_name": folder_name,
        "space_id": resolved_space_id,
        "space_name": space_name,
        "archived": archived,
        "task_count": task_count,
        "due_date": due_date,
    }

    return ConnectorDocument(
        id=stable,
        source="clickup",
        type="task_list",
        title=name,
        content="\n".join(content_parts),
        source_url="",
        metadata=metadata,
    )


# ── Retry helper ──────────────────────────────────────────────────────────────


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Execute an async callable with exponential backoff + jitter.

    Auth errors are never retried — they require human intervention.
    """
    last_exc: ClickUpError | Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ClickUpAuthError:
            raise  # never retry auth failures
        except ClickUpRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ClickUpError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except Exception as exc:
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
