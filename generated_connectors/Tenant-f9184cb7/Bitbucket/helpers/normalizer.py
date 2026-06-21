"""Normalize raw Bitbucket Cloud API resources into NormalizedDocument.

`NormalizedDocument.id = f"{tenant_id}_{source_id}"` per task spec — IDs
are scoped to the tenant so the same Bitbucket resource indexed by two
tenants never collides in the KB.
"""
from datetime import datetime
from typing import Any, Dict, Optional

import structlog
from shared.base_connector import NormalizedDocument

logger = structlog.get_logger(__name__)


def _parse_iso8601(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        # Bitbucket timestamps look like "2024-01-15T10:00:00.123456+00:00"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _author(raw: Dict[str, Any]) -> str:
    """Pluck a human-readable name from a Bitbucket user-shape dict.

    Bitbucket nests the actor under `.user` on issues and `.author` on PRs;
    accept either via the *raw* kwarg the caller passes in.
    """
    user = raw.get("user") if isinstance(raw, dict) else None
    if isinstance(user, dict):
        return (
            user.get("display_name")
            or user.get("nickname")
            or user.get("username")
            or ""
        )
    if isinstance(raw, dict):
        return (
            raw.get("display_name")
            or raw.get("nickname")
            or raw.get("username")
            or ""
        )
    return ""


def normalize_pull_request(
    pr: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    workspace: str = "",
    repo_slug: str = "",
) -> NormalizedDocument:
    """Turn a Bitbucket pull-request payload into a NormalizedDocument."""
    pr_id = pr.get("id")
    source_id = f"pr-{pr_id}"
    title = pr.get("title") or f"Pull Request #{pr_id}"
    description = (
        pr.get("description")
        or (pr.get("summary") or {}).get("raw", "")
        or ""
    )
    state = pr.get("state", "")
    source = ((pr.get("source") or {}).get("branch") or {}).get("name", "")
    destination = ((pr.get("destination") or {}).get("branch") or {}).get("name", "")

    created = _parse_iso8601(pr.get("created_on"))
    updated = _parse_iso8601(pr.get("updated_on"))
    author = _author(pr.get("author") or {})

    links = pr.get("links", {}) or {}
    url = ((links.get("html") or {}).get("href") or "")

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=description,
        content_type="text",
        source_url=url,
        author=author,
        created_at=created,
        updated_at=updated,
        source="bitbucket",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "pull_request",
            "state": state,
            "source_branch": source,
            "destination_branch": destination,
            "workspace": workspace,
            "repo_slug": repo_slug,
        },
    )


def normalize_issue(
    issue: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    workspace: str = "",
    repo_slug: str = "",
) -> NormalizedDocument:
    """Turn a Bitbucket issue payload into a NormalizedDocument."""
    issue_id = issue.get("id")
    source_id = f"issue-{issue_id}"
    title = issue.get("title") or f"Issue #{issue_id}"
    content = ""
    raw_content = issue.get("content")
    if isinstance(raw_content, dict):
        content = raw_content.get("raw", "") or raw_content.get("html", "")

    state = issue.get("state", "")
    kind = issue.get("kind", "")
    priority = issue.get("priority", "")

    created = _parse_iso8601(issue.get("created_on"))
    updated = _parse_iso8601(issue.get("updated_on"))
    reporter = _author({"user": issue.get("reporter")})

    links = issue.get("links", {}) or {}
    url = ((links.get("html") or {}).get("href") or "")

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        source_url=url,
        author=reporter,
        created_at=created,
        updated_at=updated,
        source="bitbucket",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "issue",
            "state": state,
            "issue_kind": kind,
            "priority": priority,
            "workspace": workspace,
            "repo_slug": repo_slug,
        },
    )


def normalize_repository(
    repo: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Turn a Bitbucket repository payload into a NormalizedDocument."""
    uuid = repo.get("uuid", "")
    full_name = repo.get("full_name", "")
    name = repo.get("name") or full_name
    description = repo.get("description", "") or ""
    workspace_slug = (repo.get("workspace") or {}).get("slug", "")

    created = _parse_iso8601(repo.get("created_on"))
    updated = _parse_iso8601(repo.get("updated_on"))

    links = repo.get("links", {}) or {}
    url = ((links.get("html") or {}).get("href") or "")

    source_id = f"repo-{uuid or full_name}"

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=full_name or source_id,
        title=name,
        content=description,
        content_type="text",
        source_url=url,
        author="",
        created_at=created,
        updated_at=updated,
        source="bitbucket",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "repository",
            "workspace": workspace_slug,
            "is_private": repo.get("is_private", True),
            "language": repo.get("language", ""),
        },
    )
