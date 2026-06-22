from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import BugsnagAuthError, BugsnagError, BugsnagRateLimitError
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
    last_exc: BugsnagError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except BugsnagAuthError:
            raise
        except BugsnagRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except BugsnagError as exc:
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


def normalize_error(raw: dict[str, Any], project_id: str = "") -> ConnectorDocument:
    """Convert a raw Bugsnag error into a ConnectorDocument.

    The source_id is a stable 16-char SHA-256 prefix of "error:<id>".
    """
    error_id: str = str(raw.get("id", ""))
    error_class: str = raw.get("error_class", "") or f"Error {error_id}"
    message: str = raw.get("message", "") or ""
    title: str = f"{error_class}: {message}" if message else error_class

    severity: str = raw.get("severity", "")
    status: str = raw.get("status", "")
    first_seen: str = raw.get("first_seen", "")
    last_seen: str = raw.get("last_seen", "")
    events_count: int = raw.get("events", 0) if isinstance(raw.get("events"), int) else 0
    users_count: int = raw.get("users", 0) if isinstance(raw.get("users"), int) else 0

    project_url: str = raw.get("url", "") or ""

    content_parts = [f"Error class: {error_class}"]
    if message:
        content_parts.append(f"Message: {message}")
    if severity:
        content_parts.append(f"Severity: {severity}")
    if status:
        content_parts.append(f"Status: {status}")
    if project_id:
        content_parts.append(f"Project ID: {project_id}")
    content_parts.append(f"Events: {events_count}")
    content_parts.append(f"Affected users: {users_count}")
    if first_seen:
        content_parts.append(f"First seen: {first_seen}")
    if last_seen:
        content_parts.append(f"Last seen: {last_seen}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"error:{error_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=project_url,
        metadata={
            "error_id": error_id,
            "error_class": error_class,
            "message": message,
            "severity": severity,
            "status": status,
            "project_id": project_id,
            "events_count": events_count,
            "users_count": users_count,
            "first_seen": first_seen,
            "last_seen": last_seen,
        },
    )


def normalize_project(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Bugsnag project into a ConnectorDocument.

    The source_id is a stable 16-char SHA-256 prefix of "project:<id>".
    """
    project_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "") or f"Project {project_id}"
    slug: str = raw.get("slug", "") or ""
    created_at: str = raw.get("created_at", "")
    updated_at: str = raw.get("updated_at", "")
    language: str = raw.get("language", "") or ""
    url: str = raw.get("url", "") or ""
    html_url: str = raw.get("html_url", "") or ""

    content_parts = [f"Project: {name}"]
    if slug:
        content_parts.append(f"Slug: {slug}")
    if language:
        content_parts.append(f"Language: {language}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"project:{project_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=html_url or url,
        metadata={
            "project_id": project_id,
            "name": name,
            "slug": slug,
            "language": language,
            "created_at": created_at,
            "updated_at": updated_at,
            "url": url,
            "html_url": html_url,
        },
    )


def normalize_release(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Bugsnag release into a ConnectorDocument.

    The source_id is a stable 16-char SHA-256 prefix of
    "release:<version>:<id>" where id defaults to "".
    """
    version: str = str(raw.get("version", ""))
    release_id: str = str(raw.get("id", ""))
    released_at: str = raw.get("released_at", "") or ""
    release_stage: str = raw.get("release_stage", "") or ""
    builder_name: str = raw.get("builder_name", "") or ""
    source_control_provider: str = raw.get("source_control_provider", "") or ""
    source_control_revision: str = raw.get("source_control_revision", "") or ""

    title = f"Release {version}" if version else f"Release {release_id}"

    content_parts = [f"Version: {version}"]
    if release_stage:
        content_parts.append(f"Release stage: {release_stage}")
    if builder_name:
        content_parts.append(f"Released by: {builder_name}")
    if source_control_provider:
        content_parts.append(f"Source control: {source_control_provider}")
    if source_control_revision:
        content_parts.append(f"Revision: {source_control_revision}")
    if released_at:
        content_parts.append(f"Released at: {released_at}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"release:{version}:{release_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "version": version,
            "release_id": release_id,
            "release_stage": release_stage,
            "builder_name": builder_name,
            "source_control_provider": source_control_provider,
            "source_control_revision": source_control_revision,
            "released_at": released_at,
        },
    )
