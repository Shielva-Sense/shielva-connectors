from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import AsanaAuthError, AsanaError, AsanaRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(prefix: str, gid: str) -> str:
    """Return a 16-character hex digest: sha256("{prefix}:{gid}")[:16]."""
    return hashlib.sha256(f"{prefix}:{gid}".encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_task(
    task: dict[str, Any],
    project_gid: str,
) -> ConnectorDocument:
    """Convert a raw Asana task into a ConnectorDocument.

    id  = sha256("task:" + task["gid"])[:16]
    source = "asana"
    type   = "task"
    """
    task_gid: str = task.get("gid", "") or ""
    name: str = task.get("name", "") or f"Task {task_gid}"
    notes: str = task.get("notes", "") or ""
    completed: bool = bool(task.get("completed", False))
    due_on: str = task.get("due_on", "") or ""
    created_at: str = task.get("created_at", "") or ""

    assignee = task.get("assignee") or {}
    assignee_gid: str = assignee.get("gid", "") if isinstance(assignee, dict) else ""
    assignee_name: str = assignee.get("name", "") if isinstance(assignee, dict) else ""

    content_parts: list[str] = [f"Task: {name}"]
    if notes:
        content_parts.append(f"Notes:\n{notes}")
    if assignee_name:
        content_parts.append(f"Assignee: {assignee_name}")
    if due_on:
        content_parts.append(f"Due: {due_on}")
    content_parts.append(f"Status: {'completed' if completed else 'open'}")

    doc_id = _short_id("task", task_gid) if task_gid else _short_id("task", name)
    source_url = (
        f"https://app.asana.com/0/{project_gid}/{task_gid}"
        if project_gid and task_gid
        else f"https://app.asana.com/0/0/{task_gid}"
        if task_gid
        else ""
    )

    return ConnectorDocument(
        id=doc_id,
        source="asana",
        type="task",
        title=name,
        content="\n".join(content_parts),
        source_url=source_url,
        metadata={
            "task_gid": task_gid,
            "project_gid": project_gid,
            "completed": completed,
            "due_on": due_on,
            "assignee_gid": assignee_gid,
            "assignee_name": assignee_name,
            "created_at": created_at,
        },
    )


def normalize_project(
    project: dict[str, Any],
    workspace_gid: str,
) -> ConnectorDocument:
    """Convert a raw Asana project into a ConnectorDocument.

    id  = sha256("project:" + project["gid"])[:16]
    source = "asana"
    type   = "project"
    """
    project_gid: str = project.get("gid", "") or ""
    name: str = project.get("name", "") or f"Project {project_gid}"
    notes: str = project.get("notes", "") or ""
    color: str = project.get("color", "") or ""
    created_at: str = project.get("created_at", "") or ""

    content_parts: list[str] = [f"Project: {name}"]
    if notes:
        content_parts.append(f"Notes:\n{notes}")
    if color:
        content_parts.append(f"Color: {color}")

    doc_id = _short_id("project", project_gid) if project_gid else _short_id("project", name)
    source_url = (
        f"https://app.asana.com/0/{project_gid}/list"
        if project_gid
        else ""
    )

    return ConnectorDocument(
        id=doc_id,
        source="asana",
        type="project",
        title=name,
        content="\n".join(content_parts),
        source_url=source_url,
        metadata={
            "project_gid": project_gid,
            "workspace_gid": workspace_gid,
            "color": color,
            "created_at": created_at,
        },
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
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: AsanaError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except AsanaAuthError:
            raise  # no retry on auth failures
        except AsanaRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except AsanaError as exc:
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
