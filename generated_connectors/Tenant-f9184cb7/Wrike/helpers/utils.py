"""Wrike connector normalizers and retry helper."""
from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import WrikeAuthError, WrikeError, WrikeRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(prefix: str, value: str) -> str:
    """Return a 16-character hex digest (sha256 prefix) for a typed value.

    The prefix namespaces the id so that task:abc and folder:abc never collide.
    """
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_task(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Wrike task dict into a ConnectorDocument.

    The stable ``source_id`` is sha256("task:" + task_id)[:16].
    """
    task_id: str = raw.get("id", "") or ""
    title: str = raw.get("title", "") or f"Task {task_id}"
    description: str = raw.get("description", "") or ""
    status: str = raw.get("status", "") or ""
    importance: str = raw.get("importance", "") or ""
    created_date: str = raw.get("createdDate", "") or ""
    updated_date: str = raw.get("updatedDate", "") or ""

    dates: dict[str, Any] = raw.get("dates", {}) or {}
    due_date: str = dates.get("due", "") or ""
    start_date: str = dates.get("start", "") or ""

    responsible_ids: list[str] = raw.get("responsibleIds", []) or []
    parent_ids: list[str] = raw.get("parentIds", []) or []
    custom_status_id: str = raw.get("customStatusId", "") or ""

    content_parts: list[str] = [f"Task: {title}"]
    if description:
        content_parts.append(f"Description:\n{description}")
    if status:
        content_parts.append(f"Status: {status}")
    if importance:
        content_parts.append(f"Importance: {importance}")
    if due_date:
        content_parts.append(f"Due: {due_date}")
    if start_date:
        content_parts.append(f"Start: {start_date}")
    if responsible_ids:
        content_parts.append(f"Assignees: {', '.join(responsible_ids)}")

    source_id = _short_id("task", task_id) if task_id else _short_id("task", title)
    source_url = f"https://www.wrike.com/open.htm?id={task_id}" if task_id else ""

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "task_id": task_id,
            "status": status,
            "importance": importance,
            "due_date": due_date,
            "start_date": start_date,
            "created_date": created_date,
            "updated_date": updated_date,
            "responsible_ids": responsible_ids,
            "parent_ids": parent_ids,
            "custom_status_id": custom_status_id,
        },
    )


def normalize_folder(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Wrike folder dict into a ConnectorDocument.

    The stable ``source_id`` is sha256("folder:" + folder_id)[:16].
    """
    folder_id: str = raw.get("id", "") or ""
    title: str = raw.get("title", "") or f"Folder {folder_id}"
    description: str = raw.get("description", "") or ""
    color: str = raw.get("color", "") or ""
    created_date: str = raw.get("createdDate", "") or ""
    updated_date: str = raw.get("updatedDate", "") or ""
    scope: str = raw.get("scope", "") or ""
    project: dict[str, Any] = raw.get("project", {}) or {}
    child_ids: list[str] = raw.get("childIds", []) or []

    content_parts: list[str] = [f"Folder: {title}"]
    if description:
        content_parts.append(f"Description:\n{description}")
    if color:
        content_parts.append(f"Color: {color}")
    if scope:
        content_parts.append(f"Scope: {scope}")
    if project.get("status"):
        content_parts.append(f"Project Status: {project['status']}")
    if child_ids:
        content_parts.append(f"Children: {len(child_ids)} item(s)")

    source_id = _short_id("folder", folder_id) if folder_id else _short_id("folder", title)
    source_url = f"https://www.wrike.com/open.htm?id={folder_id}" if folder_id else ""

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "folder_id": folder_id,
            "color": color,
            "scope": scope,
            "created_date": created_date,
            "updated_date": updated_date,
            "child_ids": child_ids,
            "is_project": bool(project),
            "project_status": project.get("status", ""),
        },
    )


def normalize_user(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Wrike contact/user dict into a ConnectorDocument.

    The stable ``source_id`` is sha256("user:" + user_id)[:16].
    """
    user_id: str = raw.get("id", "") or ""
    first_name: str = raw.get("firstName", "") or ""
    last_name: str = raw.get("lastName", "") or ""
    full_name: str = " ".join(p for p in [first_name, last_name] if p).strip()

    # Extract email from profiles list
    profiles: list[dict[str, Any]] = raw.get("profiles", []) or []
    email = ""
    role = ""
    if profiles and isinstance(profiles[0], dict):
        email = profiles[0].get("email", "") or ""
        role = profiles[0].get("role", "") or ""

    avatar_url: str = raw.get("avatarUrl", "") or ""
    active: bool = bool(raw.get("active", True))

    display_name = full_name or email or user_id
    content_parts: list[str] = [f"User: {display_name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if role:
        content_parts.append(f"Role: {role}")
    content_parts.append(f"Active: {'Yes' if active else 'No'}")

    source_id = _short_id("user", user_id) if user_id else _short_id("user", email or display_name)
    source_url = ""  # Wrike does not expose public user profile URLs

    return ConnectorDocument(
        source_id=source_id,
        title=display_name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "user_id": user_id,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "role": role,
            "active": active,
            "avatar_url": avatar_url,
        },
    )


def normalize_comment(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Wrike comment dict into a ConnectorDocument.

    The stable ``source_id`` is sha256("comment:" + comment_id)[:16].
    """
    comment_id: str = raw.get("id", "") or ""
    author_id: str = raw.get("authorId", "") or ""
    text: str = raw.get("text", "") or ""
    created_date: str = raw.get("createdDate", "") or ""
    updated_date: str = raw.get("updatedDate", "") or ""
    task_id: str = raw.get("taskId", "") or ""
    folder_id: str = raw.get("folderId", "") or ""

    title = text[:80].strip() or f"Comment {comment_id}"
    content_parts: list[str] = [f"Comment by user {author_id}:"]
    if text:
        content_parts.append(text)
    if created_date:
        content_parts.append(f"Created: {created_date}")
    if task_id:
        content_parts.append(f"Task: {task_id}")
    elif folder_id:
        content_parts.append(f"Folder: {folder_id}")

    source_id = _short_id("comment", comment_id) if comment_id else _short_id("comment", text)
    source_url = (
        f"https://www.wrike.com/open.htm?id={task_id}"
        if task_id
        else (f"https://www.wrike.com/open.htm?id={folder_id}" if folder_id else "")
    )

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "comment_id": comment_id,
            "author_id": author_id,
            "created_date": created_date,
            "updated_date": updated_date,
            "task_id": task_id,
            "folder_id": folder_id,
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

    Auth errors (WrikeAuthError) are never retried — they require human
    intervention (e.g. re-authorize OAuth). Rate-limit errors honour the
    retry_after value when present (WrikeRateLimitError.retry_after).
    """
    last_exc: WrikeError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except WrikeAuthError:
            raise  # no retry on auth failures
        except WrikeRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except WrikeError as exc:
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
