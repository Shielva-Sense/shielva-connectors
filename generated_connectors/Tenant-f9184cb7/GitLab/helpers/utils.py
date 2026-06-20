from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import GitLabAuthError, GitLabError, GitLabRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(entity_type: str, raw_id: str) -> str:
    """SHA-256('entity_type:raw_id')[:16] — stable, collision-resistant source ID."""
    raw = f"{entity_type}:{raw_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


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
    Rate-limit errors honour retry_after when > 0.
    """
    last_exc: GitLabError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except GitLabAuthError:
            raise
        except GitLabRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except GitLabError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


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


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_project(
    record: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw GitLab project object into a ConnectorDocument.

    Stable source_id = sha256('project:' + str(id))[:16]
    """
    raw_id: int = record.get("id", 0)
    source_id = _stable_id("project", str(raw_id))

    name: str = record.get("name", "") or f"Project {raw_id}"
    path_with_ns: str = record.get("path_with_namespace", "") or record.get("path", "") or name
    description: str = record.get("description", "") or ""
    web_url: str = record.get("web_url", "") or ""
    visibility: str = record.get("visibility", "") or ""
    default_branch: str = record.get("default_branch", "") or "main"
    star_count: int = record.get("star_count", 0) or 0
    forks_count: int = record.get("forks_count", 0) or 0
    open_issues: int = record.get("open_issues_count", 0) or 0
    created_at: str = record.get("created_at", "") or ""
    last_activity_at: str = record.get("last_activity_at", "") or ""
    namespace: dict[str, Any] = record.get("namespace", {}) or {}
    namespace_name: str = namespace.get("name", "") or ""
    topics: list[str] = record.get("topics", []) or record.get("tag_list", []) or []

    title = f"GitLab project: {path_with_ns}"
    content_parts = [
        f"Project: {path_with_ns}",
        f"Name: {name}",
        f"Description: {description}" if description else "",
        f"Namespace: {namespace_name}" if namespace_name else "",
        f"Default branch: {default_branch}",
        f"Visibility: {visibility}",
        f"Stars: {star_count}",
        f"Forks: {forks_count}",
        f"Open issues: {open_issues}",
        f"Topics: {', '.join(topics)}" if topics else "",
        f"Created: {created_at}",
        f"Last activity: {last_activity_at}",
        f"URL: {web_url}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=web_url,
        metadata={
            "entity_type": "project",
            "gitlab_id": raw_id,
            "path_with_namespace": path_with_ns,
            "name": name,
            "description": description,
            "visibility": visibility,
            "default_branch": default_branch,
            "star_count": star_count,
            "forks_count": forks_count,
            "open_issues_count": open_issues,
            "topics": topics,
            "namespace": namespace_name,
            "created_at": created_at,
            "last_activity_at": last_activity_at,
        },
    )


def normalize_issue(
    record: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw GitLab issue object into a ConnectorDocument.

    Stable source_id = sha256('issue:' + str(id))[:16]
    """
    raw_id: int = record.get("id", 0)
    source_id = _stable_id("issue", str(raw_id))

    iid: int = record.get("iid", 0)
    title_text: str = record.get("title", "") or f"Issue !{iid}"
    state: str = record.get("state", "") or ""
    description: str = record.get("description", "") or ""
    web_url: str = record.get("web_url", "") or ""
    author: dict[str, Any] = record.get("author", {}) or {}
    author_name: str = author.get("username", "") or author.get("name", "") or ""
    labels: list[str] = record.get("labels", []) or []
    created_at: str = record.get("created_at", "") or ""
    updated_at: str = record.get("updated_at", "") or ""
    closed_at: str = record.get("closed_at", "") or ""
    user_notes_count: int = record.get("user_notes_count", 0) or 0
    upvotes: int = record.get("upvotes", 0) or 0
    severity: str = record.get("severity", "") or ""
    issue_type: str = record.get("type", "") or record.get("issue_type", "") or "issue"
    milestone: dict[str, Any] = record.get("milestone", {}) or {}
    milestone_title: str = milestone.get("title", "") or "" if milestone else ""
    project_id: int = record.get("project_id", 0) or 0

    title = f"GitLab issue #{iid}: {title_text}"
    content_parts = [
        f"Issue #{iid}: {title_text}",
        f"Project ID: {project_id}" if project_id else "",
        f"State: {state}",
        f"Type: {issue_type}" if issue_type and issue_type != "issue" else "",
        f"Author: {author_name}",
        f"Labels: {', '.join(labels)}" if labels else "",
        f"Severity: {severity}" if severity else "",
        f"Milestone: {milestone_title}" if milestone_title else "",
        f"Comments: {user_notes_count}",
        f"Upvotes: {upvotes}" if upvotes else "",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
        f"Closed: {closed_at}" if closed_at else "",
        f"\n{description}" if description else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=web_url,
        metadata={
            "entity_type": "issue",
            "gitlab_id": raw_id,
            "iid": iid,
            "project_id": project_id,
            "state": state,
            "author": author_name,
            "labels": labels,
            "severity": severity,
            "issue_type": issue_type,
            "milestone": milestone_title,
            "user_notes_count": user_notes_count,
            "upvotes": upvotes,
            "created_at": created_at,
            "updated_at": updated_at,
            "closed_at": closed_at,
        },
    )


def normalize_merge_request(
    record: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw GitLab merge request object into a ConnectorDocument.

    Stable source_id = sha256('merge_request:' + str(id))[:16]
    """
    raw_id: int = record.get("id", 0)
    source_id = _stable_id("merge_request", str(raw_id))

    iid: int = record.get("iid", 0)
    title_text: str = record.get("title", "") or f"MR !{iid}"
    state: str = record.get("state", "") or ""
    description: str = record.get("description", "") or ""
    web_url: str = record.get("web_url", "") or ""
    author: dict[str, Any] = record.get("author", {}) or {}
    author_name: str = author.get("username", "") or author.get("name", "") or ""
    source_branch: str = record.get("source_branch", "") or ""
    target_branch: str = record.get("target_branch", "") or ""
    merged: bool = state == "merged"
    merged_at: str = record.get("merged_at", "") or ""
    created_at: str = record.get("created_at", "") or ""
    updated_at: str = record.get("updated_at", "") or ""
    closed_at: str = record.get("closed_at", "") or ""
    draft: bool = record.get("draft", False) or record.get("work_in_progress", False)
    labels: list[str] = record.get("labels", []) or []
    user_notes_count: int = record.get("user_notes_count", 0) or 0
    upvotes: int = record.get("upvotes", 0) or 0
    project_id: int = record.get("project_id", 0) or 0
    milestone: dict[str, Any] = record.get("milestone", {}) or {}
    milestone_title: str = milestone.get("title", "") or "" if milestone else ""
    sha: str = record.get("sha", "") or ""

    title = f"GitLab MR !{iid}: {title_text}"
    content_parts = [
        f"Merge Request !{iid}: {title_text}",
        f"Project ID: {project_id}" if project_id else "",
        f"State: {state}",
        f"Author: {author_name}",
        f"Branch: {source_branch} → {target_branch}" if source_branch and target_branch else "",
        f"Draft: {draft}" if draft else "",
        f"Labels: {', '.join(labels)}" if labels else "",
        f"Milestone: {milestone_title}" if milestone_title else "",
        f"Comments: {user_notes_count}",
        f"Upvotes: {upvotes}" if upvotes else "",
        f"SHA: {sha[:8]}" if sha else "",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
        f"Merged: {merged_at}" if merged_at else "",
        f"Closed: {closed_at}" if closed_at and not merged_at else "",
        f"\n{description}" if description else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=web_url,
        metadata={
            "entity_type": "merge_request",
            "gitlab_id": raw_id,
            "iid": iid,
            "project_id": project_id,
            "state": state,
            "merged": merged,
            "author": author_name,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "draft": draft,
            "labels": labels,
            "milestone": milestone_title,
            "user_notes_count": user_notes_count,
            "upvotes": upvotes,
            "sha": sha,
            "created_at": created_at,
            "updated_at": updated_at,
            "merged_at": merged_at,
            "closed_at": closed_at,
        },
    )


def normalize_pipeline(
    record: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw GitLab pipeline object into a ConnectorDocument.

    Stable source_id = sha256('pipeline:' + str(id))[:16]
    """
    raw_id: int = record.get("id", 0)
    source_id = _stable_id("pipeline", str(raw_id))

    status: str = record.get("status", "") or ""
    ref: str = record.get("ref", "") or ""
    sha: str = record.get("sha", "") or ""
    web_url: str = record.get("web_url", "") or ""
    created_at: str = record.get("created_at", "") or ""
    updated_at: str = record.get("updated_at", "") or ""
    started_at: str = record.get("started_at", "") or ""
    finished_at: str = record.get("finished_at", "") or ""
    duration: int | None = record.get("duration")
    project_id: int = record.get("project_id", 0) or 0
    source_trigger: str = record.get("source", "") or ""
    coverage: str | None = record.get("coverage")

    title = f"GitLab pipeline #{raw_id}: {status} on {ref}"
    content_parts = [
        f"Pipeline #{raw_id}",
        f"Project ID: {project_id}" if project_id else "",
        f"Status: {status}",
        f"Ref: {ref}",
        f"SHA: {sha[:8]}" if sha else "",
        f"Source: {source_trigger}" if source_trigger else "",
        f"Coverage: {coverage}%" if coverage else "",
        f"Duration: {duration}s" if duration is not None else "",
        f"Created: {created_at}",
        f"Started: {started_at}" if started_at else "",
        f"Finished: {finished_at}" if finished_at else "",
        f"Updated: {updated_at}",
        f"URL: {web_url}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=web_url,
        metadata={
            "entity_type": "pipeline",
            "gitlab_id": raw_id,
            "project_id": project_id,
            "status": status,
            "ref": ref,
            "sha": sha,
            "source": source_trigger,
            "coverage": coverage,
            "duration": duration,
            "created_at": created_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "updated_at": updated_at,
        },
    )


def normalize_group(
    record: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw GitLab group object into a ConnectorDocument.

    Stable source_id = sha256('group:' + str(id))[:16]
    """
    raw_id: int = record.get("id", 0)
    source_id = _stable_id("group", str(raw_id))

    name: str = record.get("name", "") or f"Group {raw_id}"
    full_path: str = record.get("full_path", "") or record.get("path", "") or name
    description: str = record.get("description", "") or ""
    web_url: str = record.get("web_url", "") or ""
    visibility: str = record.get("visibility", "") or ""
    full_name: str = record.get("full_name", "") or name
    projects_count: int = record.get("projects_count", 0) or 0
    subgroups_count: int = record.get("subgroups_count", 0) or 0
    members_count: int = record.get("statistics", {}).get("members_count", 0) if record.get("statistics") else 0
    created_at: str = record.get("created_at", "") or ""
    parent_id: int | None = record.get("parent_id")

    title = f"GitLab group: {full_path}"
    content_parts = [
        f"Group: {full_name}",
        f"Path: {full_path}",
        f"Description: {description}" if description else "",
        f"Visibility: {visibility}",
        f"Projects: {projects_count}" if projects_count else "",
        f"Subgroups: {subgroups_count}" if subgroups_count else "",
        f"Members: {members_count}" if members_count else "",
        f"Parent ID: {parent_id}" if parent_id else "",
        f"Created: {created_at}",
        f"URL: {web_url}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=web_url,
        metadata={
            "entity_type": "group",
            "gitlab_id": raw_id,
            "name": name,
            "full_name": full_name,
            "full_path": full_path,
            "description": description,
            "visibility": visibility,
            "projects_count": projects_count,
            "subgroups_count": subgroups_count,
            "parent_id": parent_id,
            "created_at": created_at,
        },
    )
