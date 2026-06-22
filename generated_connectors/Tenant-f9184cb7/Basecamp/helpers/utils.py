"""Normalizers and retry helper for the Basecamp connector.

All normalizers produce a ConnectorDocument with:
  - source_id  = sha256("<type>:<id>")[:16]  — stable, collision-resistant
  - title      = human name of the resource
  - content    = searchable plain-text body
  - source_url = deep link into Basecamp (from "app_url" field)
  - metadata   = structured Basecamp fields for downstream filtering
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import BasecampAuthError, BasecampError, BasecampRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

# Match HTML tags for stripping rich-text content
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _short_id(prefix: str, value: str) -> str:
    """Return 16-char hex digest of sha256('<prefix>:<value>')."""
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:16]


def _strip_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace for plain-text content."""
    text = _HTML_TAG_RE.sub(" ", html)
    return " ".join(text.split())


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_project(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Basecamp project dict into a ConnectorDocument."""
    project_id: int = int(raw.get("id", 0))
    name: str = str(raw.get("name", "") or f"Project {project_id}")
    description: str = str(raw.get("description", "") or "")
    app_url: str = str(raw.get("app_url", "") or "")
    status: str = str(raw.get("status", "") or "")
    created_at: str = str(raw.get("created_at", "") or "")
    updated_at: str = str(raw.get("updated_at", "") or "")
    purpose: str = str(raw.get("purpose", "") or "")

    content_parts: list[str] = [f"Project: {name}"]
    if description:
        content_parts.append(f"Description: {_strip_html(description)}")
    if purpose:
        content_parts.append(f"Purpose: {purpose}")
    if status:
        content_parts.append(f"Status: {status}")

    return ConnectorDocument(
        source_id=_short_id("project", str(project_id)),
        title=name,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=app_url,
        metadata={
            "resource_type": "project",
            "project_id": project_id,
            "status": status,
            "purpose": purpose,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_todo(raw: dict[str, Any], project_id: int) -> ConnectorDocument:
    """Convert a raw Basecamp to-do dict into a ConnectorDocument."""
    todo_id: int = int(raw.get("id", 0))
    title: str = str(raw.get("title", "") or f"Todo {todo_id}")
    content: str = str(raw.get("content", "") or "")
    app_url: str = str(raw.get("app_url", "") or "")
    completed: bool = bool(raw.get("completed", False))
    due_on: str = str(raw.get("due_on", "") or "")
    created_at: str = str(raw.get("created_at", "") or "")
    updated_at: str = str(raw.get("updated_at", "") or "")
    todolist_id: int = int(raw.get("todolist_id", 0))

    assignees: list[dict[str, Any]] = raw.get("assignees", []) or []
    assignee_names: list[str] = [
        str(a.get("name", "")) for a in assignees if isinstance(a, dict)
    ]

    content_parts: list[str] = [f"Todo: {title}"]
    if content:
        content_parts.append(f"Notes: {_strip_html(content)}")
    if assignee_names:
        content_parts.append(f"Assignees: {', '.join(assignee_names)}")
    if due_on:
        content_parts.append(f"Due: {due_on}")
    content_parts.append(f"Status: {'completed' if completed else 'open'}")

    return ConnectorDocument(
        source_id=_short_id("todo", str(todo_id)),
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=app_url,
        metadata={
            "resource_type": "todo",
            "todo_id": todo_id,
            "project_id": project_id,
            "todolist_id": todolist_id,
            "completed": completed,
            "due_on": due_on,
            "assignees": assignee_names,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_message(raw: dict[str, Any], project_id: int) -> ConnectorDocument:
    """Convert a raw Basecamp message dict into a ConnectorDocument."""
    message_id: int = int(raw.get("id", 0))
    subject: str = str(raw.get("subject", "") or f"Message {message_id}")
    body: str = str(raw.get("content", "") or "")
    app_url: str = str(raw.get("app_url", "") or "")
    created_at: str = str(raw.get("created_at", "") or "")
    updated_at: str = str(raw.get("updated_at", "") or "")

    creator: dict[str, Any] = raw.get("creator", {}) or {}
    creator_name: str = str(creator.get("name", "") if isinstance(creator, dict) else "")

    content_parts: list[str] = [f"Message: {subject}"]
    if creator_name:
        content_parts.append(f"Author: {creator_name}")
    if body:
        content_parts.append(f"Body:\n{_strip_html(body)}")

    return ConnectorDocument(
        source_id=_short_id("message", str(message_id)),
        title=subject,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=app_url,
        metadata={
            "resource_type": "message",
            "message_id": message_id,
            "project_id": project_id,
            "creator_name": creator_name,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_document(raw: dict[str, Any], project_id: int) -> ConnectorDocument:
    """Convert a raw Basecamp document dict into a ConnectorDocument."""
    doc_id: int = int(raw.get("id", 0))
    title: str = str(raw.get("title", "") or f"Document {doc_id}")
    content: str = str(raw.get("content", "") or "")
    app_url: str = str(raw.get("app_url", "") or "")
    created_at: str = str(raw.get("created_at", "") or "")
    updated_at: str = str(raw.get("updated_at", "") or "")

    creator: dict[str, Any] = raw.get("creator", {}) or {}
    creator_name: str = str(creator.get("name", "") if isinstance(creator, dict) else "")

    content_parts: list[str] = [f"Document: {title}"]
    if creator_name:
        content_parts.append(f"Author: {creator_name}")
    if content:
        content_parts.append(f"Content:\n{_strip_html(content)}")

    return ConnectorDocument(
        source_id=_short_id("document", str(doc_id)),
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=app_url,
        metadata={
            "resource_type": "document",
            "document_id": doc_id,
            "project_id": project_id,
            "creator_name": creator_name,
            "created_at": created_at,
            "updated_at": updated_at,
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

    Auth errors (BasecampAuthError) are never retried — they require
    human intervention (token refresh / re-auth).
    Rate-limit errors honour Retry-After when present.
    """
    last_exc: BasecampError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except BasecampAuthError:
            raise  # no retry on auth failures
        except BasecampRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except BasecampError as exc:
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
