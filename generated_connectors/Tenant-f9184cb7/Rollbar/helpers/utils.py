from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import RollbarAuthError, RollbarError, RollbarRateLimitError
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
    last_exc: RollbarError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except RollbarAuthError:
            raise
        except RollbarRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except RollbarError as exc:
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


def normalize_item(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Rollbar item (error group) into a ConnectorDocument.

    The source_id is a stable 16-char SHA-256 prefix of "item:{id}".
    """
    item_id: str = str(raw.get("id", ""))
    title: str = (
        raw.get("title", "")
        or raw.get("last_occurrence_title", "")
        or f"Item {item_id}"
    )
    level: str = raw.get("level", "")
    status: str = raw.get("status", "")
    environment: str = raw.get("environment", "")
    first_occurrence_timestamp: int = raw.get("first_occurrence_timestamp", 0)
    last_occurrence_timestamp: int = raw.get("last_occurrence_timestamp", 0)
    occurrence_count: int = raw.get("total_occurrences", raw.get("occurrence_count", 0))
    resolved_in_version: str = raw.get("resolved_in_version", "") or ""
    assigned_user_id: int | None = raw.get("assigned_user_id")

    content_parts = [f"Title: {title}"]
    if level:
        content_parts.append(f"Level: {level}")
    if status:
        content_parts.append(f"Status: {status}")
    if environment:
        content_parts.append(f"Environment: {environment}")
    content_parts.append(f"Occurrences: {occurrence_count}")
    if first_occurrence_timestamp:
        content_parts.append(f"First seen: {first_occurrence_timestamp}")
    if last_occurrence_timestamp:
        content_parts.append(f"Last seen: {last_occurrence_timestamp}")
    if resolved_in_version:
        content_parts.append(f"Resolved in: {resolved_in_version}")
    if assigned_user_id is not None:
        content_parts.append(f"Assigned user ID: {assigned_user_id}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"item:{item_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "item_id": item_id,
            "level": level,
            "status": status,
            "environment": environment,
            "occurrence_count": occurrence_count,
            "first_occurrence_timestamp": first_occurrence_timestamp,
            "last_occurrence_timestamp": last_occurrence_timestamp,
            "resolved_in_version": resolved_in_version,
            "assigned_user_id": assigned_user_id,
        },
    )


def normalize_occurrence(occ: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Rollbar occurrence (instance) into a ConnectorDocument.

    The source_id is a stable 16-char SHA-256 prefix of "occurrence:{id}".
    """
    occ_id: str = str(occ.get("id", ""))
    item_id: str = str(occ.get("item_id", ""))
    timestamp: int = occ.get("timestamp", 0)
    environment: str = occ.get("environment", "")
    level: str = occ.get("level", "")
    language: str = occ.get("language", "") or ""
    framework: str = occ.get("framework", "") or ""

    body: dict[str, Any] = occ.get("body", {}) or {}
    # Extract message text if present
    message_obj: dict[str, Any] = body.get("message", {}) or {}
    message_body: str = message_obj.get("body", "") or ""
    # Extract exception class from trace
    trace_obj: dict[str, Any] = body.get("trace", {}) or {}
    exc_obj: dict[str, Any] = trace_obj.get("exception", {}) or {}
    exc_class: str = exc_obj.get("class", "") or ""
    exc_message: str = exc_obj.get("message", "") or ""

    title_parts = [p for p in [exc_class, exc_message, message_body] if p]
    title = ": ".join(title_parts) if title_parts else f"Occurrence {occ_id}"

    content_parts = [f"Occurrence ID: {occ_id}"]
    if item_id:
        content_parts.append(f"Item ID: {item_id}")
    if level:
        content_parts.append(f"Level: {level}")
    if environment:
        content_parts.append(f"Environment: {environment}")
    if language:
        content_parts.append(f"Language: {language}")
    if framework:
        content_parts.append(f"Framework: {framework}")
    if exc_class:
        content_parts.append(f"Exception: {exc_class}")
    if exc_message:
        content_parts.append(f"Message: {exc_message}")
    if timestamp:
        content_parts.append(f"Timestamp: {timestamp}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"occurrence:{occ_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "occurrence_id": occ_id,
            "item_id": item_id,
            "timestamp": timestamp,
            "environment": environment,
            "level": level,
            "language": language,
            "framework": framework,
            "exc_class": exc_class,
            "exc_message": exc_message,
        },
    )


def normalize_deploy(d: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Rollbar deploy into a ConnectorDocument.

    The source_id is a stable 16-char SHA-256 prefix of "deploy:{id}".
    """
    deploy_id: str = str(d.get("id", ""))
    environment: str = d.get("environment", "")
    revision: str = d.get("revision", "") or ""
    rollbar_username: str = d.get("rollbar_username", "") or ""
    local_username: str = d.get("local_username", "") or ""
    comment: str = d.get("comment", "") or ""
    status: str = d.get("status", "") or ""
    finish_time: int = d.get("finish_time", 0)
    start_time: int = d.get("start_time", 0)

    deployer = rollbar_username or local_username or "unknown"
    title = f"Deploy {deploy_id} to {environment}" if environment else f"Deploy {deploy_id}"

    content_parts = [f"Deploy: {deploy_id}"]
    if environment:
        content_parts.append(f"Environment: {environment}")
    if revision:
        content_parts.append(f"Revision: {revision}")
    if deployer != "unknown":
        content_parts.append(f"Deployed by: {deployer}")
    if status:
        content_parts.append(f"Status: {status}")
    if comment:
        content_parts.append(f"Comment: {comment}")
    if start_time:
        content_parts.append(f"Started: {start_time}")
    if finish_time:
        content_parts.append(f"Finished: {finish_time}")
    content = "\n".join(content_parts)

    source_id = _short_hash(f"deploy:{deploy_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url="",
        metadata={
            "deploy_id": deploy_id,
            "environment": environment,
            "revision": revision,
            "rollbar_username": rollbar_username,
            "local_username": local_username,
            "deployer": deployer,
            "comment": comment,
            "status": status,
            "start_time": start_time,
            "finish_time": finish_time,
        },
    )
