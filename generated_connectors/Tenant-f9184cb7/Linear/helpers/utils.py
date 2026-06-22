from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import LinearAuthError, LinearError, LinearRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

PRIORITY_LABELS: dict[int, str] = {
    0: "No Priority",
    1: "Urgent",
    2: "High",
    3: "Medium",
    4: "Low",
}


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
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: LinearError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except LinearAuthError:
            raise
        except LinearRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except LinearError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _short_hash(value: str) -> str:
    """Return a 16-character hex digest of SHA-256 for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def normalize_issue(
    issue: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw Linear issue dict into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of the issue ID so it fits
    within Shielva's canonical 16-char source_id budget while remaining
    deterministic and collision-resistant.
    """
    issue_id: str = issue.get("id", "")
    title: str = issue.get("title", "") or f"Issue {issue_id}"
    description: str = issue.get("description", "") or ""

    state: dict[str, Any] = issue.get("state", {}) or {}
    state_name: str = state.get("name", "") or "Unknown"

    priority: int = issue.get("priority", 0) or 0
    priority_label: str = PRIORITY_LABELS.get(priority, "Unknown")

    assignee: dict[str, Any] | None = issue.get("assignee")
    assignee_name: str = (assignee.get("name", "") if assignee else "") or "Unassigned"

    team: dict[str, Any] | None = issue.get("team")
    team_name: str = (team.get("name", "") if team else "") or ""
    team_key: str = (team.get("key", "") if team else "") or ""

    created_at: str = issue.get("createdAt", "") or ""
    updated_at: str = issue.get("updatedAt", "") or ""

    # Build content
    content_parts: list[str] = []
    if description:
        content_parts.append(description)
    content_parts.append(f"Status: {state_name}")
    content_parts.append(f"Priority: {priority_label}")
    if assignee_name and assignee_name != "Unassigned":
        content_parts.append(f"Assignee: {assignee_name}")
    if team_name:
        content_parts.append(f"Team: {team_name}")

    content = "\n".join(content_parts)

    source_id = _short_hash(issue_id)
    display_title = f"[{team_key}] {title}" if team_key else title
    source_url = f"https://linear.app/issue/{issue_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=display_title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "issue_id": issue_id,
            "state": state_name,
            "priority": priority,
            "priority_label": priority_label,
            "assignee": assignee_name,
            "team_name": team_name,
            "team_key": team_key,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_project(
    project: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw Linear project dict into a ConnectorDocument."""
    project_id: str = project.get("id", "")
    name: str = project.get("name", "") or f"Project {project_id}"
    description: str = project.get("description", "") or ""
    state: str = project.get("state", "") or "Unknown"

    content_parts: list[str] = []
    if description:
        content_parts.append(description)
    content_parts.append(f"State: {state}")
    content = "\n".join(content_parts)

    source_id = _short_hash(project_id)
    source_url = f"https://linear.app/project/{project_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "project_id": project_id,
            "state": state,
        },
    )
