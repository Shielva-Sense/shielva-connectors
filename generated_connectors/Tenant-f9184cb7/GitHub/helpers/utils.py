from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import GitHubAuthError, GitHubError, GitHubRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(entity_type: str, html_url: str) -> str:
    """SHA-256(entity_type + ':' + html_url)[:16] — stable, collision-resistant source ID."""
    raw = f"{entity_type}:{html_url}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the retry_after value when > 0.
    """
    last_exc: GitHubError | None = None
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except GitHubAuthError:
            raise
        except GitHubRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except GitHubError as exc:
            last_exc = exc
            if attempt + 1 == max_retries:
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


def normalize_repo(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw GitHub repository object into a ConnectorDocument."""
    html_url: str = record.get("html_url", "") or ""
    source_id = _stable_id("repo", html_url)

    full_name: str = record.get("full_name", "") or record.get("name", "") or "unknown"
    description: str = record.get("description", "") or ""
    language: str = record.get("language", "") or ""
    default_branch: str = record.get("default_branch", "") or "main"
    stars: int = record.get("stargazers_count", 0) or 0
    forks: int = record.get("forks_count", 0) or 0
    open_issues: int = record.get("open_issues_count", 0) or 0
    visibility: str = record.get("visibility", "") or ("private" if record.get("private") else "public")
    created_at: str = record.get("created_at", "") or ""
    updated_at: str = record.get("updated_at", "") or ""
    topics: list[str] = record.get("topics", []) or []

    title = f"GitHub repo: {full_name}"
    content_parts = [
        f"Repository: {full_name}",
        f"Description: {description}",
        f"Language: {language}",
        f"Default branch: {default_branch}",
        f"Stars: {stars}",
        f"Forks: {forks}",
        f"Open issues: {open_issues}",
        f"Visibility: {visibility}",
        f"Topics: {', '.join(topics)}" if topics else "",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
        f"URL: {html_url}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=html_url,
        metadata={
            "entity_type": "repo",
            "full_name": full_name,
            "description": description,
            "language": language,
            "default_branch": default_branch,
            "stars": stars,
            "forks": forks,
            "open_issues": open_issues,
            "visibility": visibility,
            "topics": topics,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_issue(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw GitHub issue object into a ConnectorDocument."""
    html_url: str = record.get("html_url", "") or ""
    source_id = _stable_id("issue", html_url)

    number: int = record.get("number", 0)
    title_text: str = record.get("title", "") or f"Issue #{number}"
    state: str = record.get("state", "") or ""
    body: str = record.get("body", "") or ""
    user: dict[str, Any] = record.get("user", {}) or {}
    author: str = user.get("login", "") or ""
    labels: list[dict[str, Any]] = record.get("labels", []) or []
    label_names: list[str] = [lbl.get("name", "") for lbl in labels if lbl.get("name")]
    created_at: str = record.get("created_at", "") or ""
    updated_at: str = record.get("updated_at", "") or ""
    closed_at: str = record.get("closed_at", "") or ""
    comments: int = record.get("comments", 0)

    # Extract repo owner/name from html_url: https://github.com/{owner}/{repo}/issues/{n}
    repo_ref = ""
    parts = html_url.split("/")
    if len(parts) >= 5:
        repo_ref = f"{parts[3]}/{parts[4]}"

    title = f"GitHub issue #{number}: {title_text}"
    content_parts = [
        f"Issue #{number}: {title_text}",
        f"Repository: {repo_ref}" if repo_ref else "",
        f"State: {state}",
        f"Author: {author}",
        f"Labels: {', '.join(label_names)}" if label_names else "",
        f"Comments: {comments}",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
        f"Closed: {closed_at}" if closed_at else "",
        f"\n{body}" if body else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=html_url,
        metadata={
            "entity_type": "issue",
            "number": number,
            "state": state,
            "author": author,
            "labels": label_names,
            "comments": comments,
            "created_at": created_at,
            "updated_at": updated_at,
            "closed_at": closed_at,
            "repo": repo_ref,
        },
    )


def normalize_pr(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw GitHub pull request object into a ConnectorDocument."""
    html_url: str = record.get("html_url", "") or ""
    source_id = _stable_id("pr", html_url)

    number: int = record.get("number", 0)
    title_text: str = record.get("title", "") or f"PR #{number}"
    state: str = record.get("state", "") or ""
    body: str = record.get("body", "") or ""
    user: dict[str, Any] = record.get("user", {}) or {}
    author: str = user.get("login", "") or ""
    merged: bool = record.get("merged", False) or bool(record.get("merged_at"))
    merged_at: str = record.get("merged_at", "") or ""
    created_at: str = record.get("created_at", "") or ""
    updated_at: str = record.get("updated_at", "") or ""
    closed_at: str = record.get("closed_at", "") or ""
    draft: bool = record.get("draft", False)
    commits: int = record.get("commits", 0)
    additions: int = record.get("additions", 0)
    deletions: int = record.get("deletions", 0)
    changed_files: int = record.get("changed_files", 0)
    labels: list[dict[str, Any]] = record.get("labels", []) or []
    label_names: list[str] = [lbl.get("name", "") for lbl in labels if lbl.get("name")]

    # head/base branch names
    head: dict[str, Any] = record.get("head", {}) or {}
    base: dict[str, Any] = record.get("base", {}) or {}
    head_ref: str = head.get("ref", "") or ""
    base_ref: str = base.get("ref", "") or ""

    repo_ref = ""
    parts = html_url.split("/")
    if len(parts) >= 5:
        repo_ref = f"{parts[3]}/{parts[4]}"

    title = f"GitHub PR #{number}: {title_text}"
    content_parts = [
        f"Pull Request #{number}: {title_text}",
        f"Repository: {repo_ref}" if repo_ref else "",
        f"State: {'merged' if merged else state}",
        f"Author: {author}",
        f"Branch: {head_ref} → {base_ref}" if head_ref and base_ref else "",
        f"Draft: {draft}",
        f"Labels: {', '.join(label_names)}" if label_names else "",
        f"Commits: {commits}",
        f"Changes: +{additions} / -{deletions} in {changed_files} files" if changed_files else "",
        f"Created: {created_at}",
        f"Updated: {updated_at}",
        f"Merged: {merged_at}" if merged_at else "",
        f"Closed: {closed_at}" if closed_at and not merged_at else "",
        f"\n{body}" if body else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=html_url,
        metadata={
            "entity_type": "pr",
            "number": number,
            "state": state,
            "merged": merged,
            "author": author,
            "head_ref": head_ref,
            "base_ref": base_ref,
            "draft": draft,
            "commits": commits,
            "additions": additions,
            "deletions": deletions,
            "changed_files": changed_files,
            "labels": label_names,
            "created_at": created_at,
            "updated_at": updated_at,
            "merged_at": merged_at,
            "closed_at": closed_at,
            "repo": repo_ref,
        },
    )
