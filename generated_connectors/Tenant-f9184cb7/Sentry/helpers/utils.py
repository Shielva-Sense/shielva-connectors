from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import SentryAuthError, SentryError, SentryRateLimitError
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

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the retry_after value when present.
    """
    last_exc: SentryError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except SentryAuthError:
            raise
        except SentryRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except SentryError as exc:
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


def _short_hash(value: str) -> str:
    """Return a 16-character hex digest of SHA-256 for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def normalize_issue(raw: dict[str, Any], org_slug: str = "") -> ConnectorDocument:
    """Convert a raw Sentry issue into a ConnectorDocument.

    The source_id is a stable 16-char SHA-256 prefix of "issue:<id>".
    """
    issue_id: str = str(raw.get("id", ""))
    title: str = raw.get("title", "") or f"Issue {issue_id}"
    culprit: str = raw.get("culprit", "") or ""
    status: str = raw.get("status", "")
    level: str = raw.get("level", "")
    first_seen: str = raw.get("firstSeen", "")
    last_seen: str = raw.get("lastSeen", "")
    times_seen: int = raw.get("timesSeen", 0)
    permalink: str = raw.get("permalink", "")

    project_obj: dict[str, Any] = raw.get("project", {}) or {}
    project_slug: str = project_obj.get("slug", "")
    project_name: str = project_obj.get("name", "")

    assignee: dict[str, Any] = raw.get("assignedTo", {}) or {}
    assignee_name: str = assignee.get("name", "") if isinstance(assignee, dict) else ""

    metadata_obj: dict[str, Any] = raw.get("metadata", {}) or {}
    error_type: str = metadata_obj.get("type", "")
    error_value: str = metadata_obj.get("value", "")

    content_parts = [f"Status: {status}", f"Level: {level}"]
    if culprit:
        content_parts.append(f"Culprit: {culprit}")
    if error_type:
        content_parts.append(f"Type: {error_type}")
    if error_value:
        content_parts.append(f"Error: {error_value}")
    if project_name:
        content_parts.append(f"Project: {project_name}")
    if assignee_name:
        content_parts.append(f"Assigned to: {assignee_name}")
    content_parts.append(f"Times seen: {times_seen}")
    if first_seen:
        content_parts.append(f"First seen: {first_seen}")
    if last_seen:
        content_parts.append(f"Last seen: {last_seen}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"issue:{issue_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=permalink,
        metadata={
            "issue_id": issue_id,
            "org_slug": org_slug,
            "project_slug": project_slug,
            "project_name": project_name,
            "status": status,
            "level": level,
            "culprit": culprit,
            "error_type": error_type,
            "error_value": error_value,
            "times_seen": times_seen,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "assignee": assignee_name,
        },
    )


def normalize_project(raw: dict[str, Any], org_slug: str = "") -> ConnectorDocument:
    """Convert a raw Sentry project into a ConnectorDocument."""
    project_id: str = str(raw.get("id", ""))
    slug: str = raw.get("slug", "")
    name: str = raw.get("name", "") or slug or f"Project {project_id}"
    status: str = raw.get("status", "")
    platform: str = raw.get("platform", "") or ""
    date_created: str = raw.get("dateCreated", "")
    is_public: bool = raw.get("isPublic", False)

    team_list: list[dict[str, Any]] = raw.get("teams", [])
    team_names: list[str] = [t.get("name", "") for t in team_list if t.get("name")]

    content_parts = [f"Project: {name}", f"Slug: {slug}"]
    if status:
        content_parts.append(f"Status: {status}")
    if platform:
        content_parts.append(f"Platform: {platform}")
    if team_names:
        content_parts.append(f"Teams: {', '.join(team_names)}")
    content_parts.append(f"Public: {is_public}")
    if date_created:
        content_parts.append(f"Created: {date_created}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"project:{project_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content=content,
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "project_id": project_id,
            "slug": slug,
            "org_slug": org_slug,
            "status": status,
            "platform": platform,
            "teams": team_names,
            "is_public": is_public,
            "date_created": date_created,
        },
    )


def normalize_release(raw: dict[str, Any], org_slug: str = "") -> ConnectorDocument:
    """Convert a raw Sentry release into a ConnectorDocument."""
    version: str = raw.get("version", "")
    short_version: str = raw.get("shortVersion", version)
    date_created: str = raw.get("dateCreated", "")
    date_released: str = raw.get("dateReleased", "") or ""
    url: str = raw.get("url", "") or ""

    authors: list[dict[str, Any]] = raw.get("authors", [])
    author_names: list[str] = [
        a.get("name", a.get("username", "")) for a in authors if a
    ]

    project_list: list[dict[str, Any]] = raw.get("projects", [])
    project_slugs: list[str] = [p.get("slug", "") for p in project_list if p.get("slug")]

    commit_count: int = raw.get("commitCount", 0)
    new_groups: int = raw.get("newGroups", 0)

    title = f"Release {short_version}"
    content_parts = [f"Version: {version}"]
    if project_slugs:
        content_parts.append(f"Projects: {', '.join(project_slugs)}")
    if author_names:
        content_parts.append(f"Authors: {', '.join(author_names)}")
    content_parts.append(f"Commits: {commit_count}")
    content_parts.append(f"New issues: {new_groups}")
    if date_created:
        content_parts.append(f"Created: {date_created}")
    if date_released:
        content_parts.append(f"Released: {date_released}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"release:{org_slug}:{version}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=url,
        metadata={
            "version": version,
            "short_version": short_version,
            "org_slug": org_slug,
            "projects": project_slugs,
            "authors": author_names,
            "commit_count": commit_count,
            "new_groups": new_groups,
            "date_created": date_created,
            "date_released": date_released,
        },
    )


def normalize_event(raw: dict[str, Any], issue_id: str = "") -> ConnectorDocument:
    """Convert a raw Sentry event into a ConnectorDocument."""
    event_id: str = str(raw.get("id", raw.get("eventID", "")))
    title: str = raw.get("title", "") or raw.get("message", "") or f"Event {event_id}"
    platform: str = raw.get("platform", "") or ""
    date_created: str = raw.get("dateCreated", "")
    culprit: str = raw.get("culprit", "") or ""
    level: str = raw.get("level", "")

    tags: list[dict[str, Any]] = raw.get("tags", [])
    tag_str = ", ".join(
        f"{t.get('key', '')}={t.get('value', '')}" for t in tags if t
    )

    content_parts = [f"Event: {title}"]
    if level:
        content_parts.append(f"Level: {level}")
    if platform:
        content_parts.append(f"Platform: {platform}")
    if culprit:
        content_parts.append(f"Culprit: {culprit}")
    if tag_str:
        content_parts.append(f"Tags: {tag_str}")
    if date_created:
        content_parts.append(f"Date: {date_created}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"event:{event_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "event_id": event_id,
            "issue_id": issue_id,
            "platform": platform,
            "level": level,
            "culprit": culprit,
            "date_created": date_created,
            "tags": tags,
        },
    )
