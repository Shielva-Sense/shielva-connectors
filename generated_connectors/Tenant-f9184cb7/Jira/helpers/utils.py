from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import JiraAuthError, JiraError, JiraRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(prefix: str, value: str) -> str:
    """Return a 16-character hex digest of SHA-256(prefix + value)."""
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:16]


# ── Issue normalizer ──────────────────────────────────────────────────────────


def normalize_issue(
    issue: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a raw Jira issue dict into a ConnectorDocument.

    Stable ``id`` = first 16 hex chars of SHA-256("issue:" + issue_id).
    ``type`` is always ``"issue"``.
    ``source`` is always ``"jira"``.
    """
    issue_id: str = issue.get("id", "") or ""
    issue_key: str = issue.get("key", "") or ""
    fields: dict[str, Any] = issue.get("fields") or {}

    # Stable ID — SHA-256 of "issue:<issue_id>", truncated to 16 hex chars
    stable_id = _short_id("issue", issue_id) if issue_id else _short_id("issue", issue_key)

    summary: str = fields.get("summary") or f"Issue {issue_key}"
    title = f"[{issue_key}] {summary}" if issue_key else summary

    # Description — Jira API v3 returns ADF; v2 returns plain text
    description_field = fields.get("description")
    if isinstance(description_field, dict):
        description_text = _extract_adf_text(description_field)
    elif isinstance(description_field, str):
        description_text = description_field
    else:
        description_text = ""

    status_name = _nested_str(fields.get("status"), "name")
    priority_name = _nested_str(fields.get("priority"), "name")
    assignee_name = _nested_str(fields.get("assignee"), "displayName")
    reporter_name = _nested_str(fields.get("reporter"), "displayName")
    project_key = _nested_str(fields.get("project"), "key")
    issuetype_name = _nested_str(fields.get("issuetype"), "name")
    labels: list[str] = fields.get("labels") or []

    content_parts: list[str] = [f"Key: {issue_key}", f"Summary: {summary}"]
    if status_name:
        content_parts.append(f"Status: {status_name}")
    if priority_name:
        content_parts.append(f"Priority: {priority_name}")
    if assignee_name:
        content_parts.append(f"Assignee: {assignee_name}")
    if reporter_name:
        content_parts.append(f"Reporter: {reporter_name}")
    if issuetype_name:
        content_parts.append(f"Type: {issuetype_name}")
    if project_key:
        content_parts.append(f"Project: {project_key}")
    if description_text:
        content_parts.append(f"Description: {description_text}")
    if labels:
        content_parts.append(f"Labels: {', '.join(labels)}")

    content = "\n".join(content_parts)
    source_url = f"https://atlassian.net/browse/{issue_key}" if issue_key else ""

    metadata: dict[str, Any] = {
        "issue_key": issue_key,
        "status": status_name,
        "priority": priority_name,
        "assignee": assignee_name,
        "reporter": reporter_name,
        "project_key": project_key,
        "issuetype": issuetype_name,
        "labels": labels,
        "created": fields.get("created", ""),
        "updated": fields.get("updated", ""),
    }

    return ConnectorDocument(
        id=stable_id,
        source_id=issue_id or issue_key,
        title=title,
        content=content,
        source="jira",
        type="issue",
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata=metadata,
    )


# ── Project normalizer ────────────────────────────────────────────────────────


def normalize_project(
    project: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Normalize a raw Jira project dict into a ConnectorDocument.

    Stable ``id`` = first 16 hex chars of SHA-256("project:" + project_id).
    ``type`` is always ``"project"``.
    ``source`` is always ``"jira"``.
    """
    project_id: str = str(project.get("id", "")) or ""
    project_key: str = project.get("key", "") or ""
    name: str = project.get("name", "") or f"Project {project_key}"
    description: str = project.get("description", "") or ""
    project_type: str = project.get("projectTypeKey", "") or ""

    stable_id = _short_id("project", project_id) if project_id else _short_id("project", project_key)

    content_parts: list[str] = [f"Project: {name}", f"Key: {project_key}"]
    if project_type:
        content_parts.append(f"Type: {project_type}")
    if description:
        content_parts.append(f"Description: {description}")

    # Lead (optional)
    lead = project.get("lead")
    if isinstance(lead, dict):
        lead_name = lead.get("displayName", "") or lead.get("name", "")
        if lead_name:
            content_parts.append(f"Lead: {lead_name}")

    source_url = (
        f"https://atlassian.net/jira/software/projects/{project_key}/boards"
        if project_key
        else ""
    )

    metadata: dict[str, Any] = {
        "project_id": project_id,
        "project_key": project_key,
        "project_type": project_type,
        "description": description,
    }

    return ConnectorDocument(
        id=stable_id,
        source_id=project_id or project_key,
        title=name,
        content="\n".join(content_parts),
        source="jira",
        type="project",
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata=metadata,
    )


# ── ADF text extractor ────────────────────────────────────────────────────────


def _extract_adf_text(node: dict[str, Any]) -> str:
    """Recursively extract plain text from an Atlassian Document Format node."""
    parts: list[str] = []
    if node.get("type") == "text":
        parts.append(node.get("text", ""))
    for child in node.get("content") or []:
        if isinstance(child, dict):
            parts.append(_extract_adf_text(child))
    return " ".join(p for p in parts if p).strip()


def _nested_str(obj: Any, key: str) -> str:
    """Safely extract a string from a nested dict."""
    if isinstance(obj, dict):
        return obj.get(key, "") or ""
    return ""


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
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: JiraError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except JiraAuthError:
            raise  # no retry on auth failures
        except JiraRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except JiraError as exc:
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
