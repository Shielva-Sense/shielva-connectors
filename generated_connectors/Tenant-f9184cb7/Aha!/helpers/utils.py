from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import AhaAuthError, AhaError, AhaRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(prefix: str, value: str) -> str:
    """Return a 16-character hex digest: sha256('{prefix}:{value}')[:16]."""
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_feature(
    f: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Aha! feature into a ConnectorDocument."""
    feature_id: str = str(f.get("id", "") or f.get("reference_num", ""))
    name: str = f.get("name", "") or f"Feature {feature_id}"
    description_obj = f.get("description") or {}
    description: str = (
        description_obj.get("body", "")
        if isinstance(description_obj, dict)
        else str(description_obj)
    ) or ""
    status: str = (f.get("workflow_status") or {}).get("name", "") or ""
    release_ref = (f.get("release") or {}).get("reference_num", "")
    created_at: str = f.get("created_at", "") or ""
    updated_at: str = f.get("updated_at", "") or ""
    reference_num: str = f.get("reference_num", "") or ""
    url: str = f.get("url", "") or ""

    content_parts: list[str] = [f"Feature: {name}"]
    if reference_num:
        content_parts.append(f"Reference: {reference_num}")
    if description:
        content_parts.append(f"Description:\n{description}")
    if status:
        content_parts.append(f"Status: {status}")
    if release_ref:
        content_parts.append(f"Release: {release_ref}")

    source_id = _short_id("feature", feature_id) if feature_id else _short_id("feature", name)

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=url,
        metadata={
            "type": "feature",
            "feature_id": feature_id,
            "reference_num": reference_num,
            "status": status,
            "release_reference_num": release_ref,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_release(
    r: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Aha! release into a ConnectorDocument."""
    release_id: str = str(r.get("id", "") or r.get("reference_num", ""))
    name: str = r.get("name", "") or f"Release {release_id}"
    release_date: str = r.get("release_date", "") or ""
    status: str = r.get("development_started_on", "") or ""
    reference_num: str = r.get("reference_num", "") or ""
    url: str = r.get("url", "") or ""
    created_at: str = r.get("created_at", "") or ""
    updated_at: str = r.get("updated_at", "") or ""

    content_parts: list[str] = [f"Release: {name}"]
    if reference_num:
        content_parts.append(f"Reference: {reference_num}")
    if release_date:
        content_parts.append(f"Release date: {release_date}")

    source_id = _short_id("release", release_id) if release_id else _short_id("release", name)

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=url,
        metadata={
            "type": "release",
            "release_id": release_id,
            "reference_num": reference_num,
            "release_date": release_date,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_idea(
    i: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Aha! idea into a ConnectorDocument."""
    idea_id: str = str(i.get("id", "") or i.get("reference_num", ""))
    name: str = i.get("name", "") or f"Idea {idea_id}"
    description_obj = i.get("description") or {}
    description: str = (
        description_obj.get("body", "")
        if isinstance(description_obj, dict)
        else str(description_obj)
    ) or ""
    status: str = (i.get("workflow_status") or {}).get("name", "") or ""
    reference_num: str = i.get("reference_num", "") or ""
    url: str = i.get("url", "") or ""
    votes: int = int(i.get("votes_count", 0) or 0)
    created_at: str = i.get("created_at", "") or ""
    updated_at: str = i.get("updated_at", "") or ""

    content_parts: list[str] = [f"Idea: {name}"]
    if reference_num:
        content_parts.append(f"Reference: {reference_num}")
    if description:
        content_parts.append(f"Description:\n{description}")
    if status:
        content_parts.append(f"Status: {status}")
    if votes:
        content_parts.append(f"Votes: {votes}")

    source_id = _short_id("idea", idea_id) if idea_id else _short_id("idea", name)

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=url,
        metadata={
            "type": "idea",
            "idea_id": idea_id,
            "reference_num": reference_num,
            "status": status,
            "votes": votes,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_goal(
    g: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Aha! goal into a ConnectorDocument."""
    goal_id: str = str(g.get("id", "") or "")
    name: str = g.get("name", "") or f"Goal {goal_id}"
    description_obj = g.get("description") or {}
    description: str = (
        description_obj.get("body", "")
        if isinstance(description_obj, dict)
        else str(description_obj)
    ) or ""
    url: str = g.get("url", "") or ""
    created_at: str = g.get("created_at", "") or ""
    updated_at: str = g.get("updated_at", "") or ""
    reference_num: str = g.get("reference_num", "") or ""

    content_parts: list[str] = [f"Goal: {name}"]
    if reference_num:
        content_parts.append(f"Reference: {reference_num}")
    if description:
        content_parts.append(f"Description:\n{description}")

    source_id = _short_id("goal", goal_id) if goal_id else _short_id("goal", name)

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=url,
        metadata={
            "type": "goal",
            "goal_id": goal_id,
            "reference_num": reference_num,
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

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: AhaError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except AhaAuthError:
            raise  # no retry on auth failures
        except AhaRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except AhaError as exc:
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
