from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import LaunchDarklyAuthError, LaunchDarklyError, LaunchDarklyRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

LD_APP_BASE = "https://app.launchdarkly.com"


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
    Rate-limit errors honour retry_after when present.
    """
    last_exc: LaunchDarklyError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except LaunchDarklyAuthError:
            raise
        except LaunchDarklyRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except LaunchDarklyError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_id(prefix: str, resource_id: str) -> str:
    """Return SHA-256(prefix + ':' + resource_id)[:16].

    Provides a stable, compact document identifier for deduplication across syncs.
    """
    raw = f"{prefix}:{resource_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_project(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a LaunchDarkly project object into a ConnectorDocument.

    Stable ID = SHA-256("project:" + key)[:16]
    """
    key: str = raw.get("key", "")
    name: str = raw.get("name", key or "Unnamed Project")
    project_id: str = raw.get("_id", key)
    tags: list[str] = raw.get("tags", [])
    include_in_snippet_by_default: bool = raw.get("includeInSnippetByDefault", False)

    source_id = _stable_id("project", key)
    content_parts = [
        f"Project key: {key}",
        f"Name: {name}",
    ]
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    content_parts.append(f"Include in snippet by default: {include_in_snippet_by_default}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"LaunchDarkly project: {name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{LD_APP_BASE}/{key}",
        metadata={
            "project_id": project_id,
            "key": key,
            "name": name,
            "tags": tags,
            "include_in_snippet_by_default": include_in_snippet_by_default,
        },
    )


def normalize_flag(raw: dict[str, Any], project_key: str = "") -> ConnectorDocument:
    """Convert a LaunchDarkly feature flag object into a ConnectorDocument.

    Stable ID = SHA-256("flag:" + project_key + ":" + key)[:16]
    """
    key: str = raw.get("key", "")
    name: str = raw.get("name", key or "Unnamed Flag")
    description: str = raw.get("description", "")
    kind: str = raw.get("kind", "boolean")
    tags: list[str] = raw.get("tags", [])
    archived: bool = raw.get("archived", False)
    temporary: bool = raw.get("temporary", False)
    maintainer_id: str = raw.get("maintainerId", "")
    creation_date: int = raw.get("creationDate", 0)
    variations: list[dict[str, Any]] = raw.get("variations", [])

    # Project key may be on the flag or passed explicitly
    proj_key = project_key or raw.get("projectKey", "")
    stable_key = f"{proj_key}:{key}" if proj_key else key
    source_id = _stable_id("flag", stable_key)

    content_parts = [
        f"Flag key: {key}",
        f"Name: {name}",
        f"Project: {proj_key}",
        f"Kind: {kind}",
        f"Archived: {archived}",
        f"Temporary: {temporary}",
    ]
    if description:
        content_parts.append(f"Description: {description}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    if maintainer_id:
        content_parts.append(f"Maintainer ID: {maintainer_id}")
    if variations:
        content_parts.append(f"Variations: {len(variations)}")
    if creation_date:
        content_parts.append(f"Created: {creation_date}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"LaunchDarkly flag: {name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{LD_APP_BASE}/{proj_key}/features/{key}",
        metadata={
            "key": key,
            "name": name,
            "project_key": proj_key,
            "kind": kind,
            "description": description,
            "tags": tags,
            "archived": archived,
            "temporary": temporary,
            "maintainer_id": maintainer_id,
            "creation_date": creation_date,
            "variation_count": len(variations),
        },
    )


def normalize_environment(raw: dict[str, Any], project_key: str = "") -> ConnectorDocument:
    """Convert a LaunchDarkly environment object into a ConnectorDocument.

    Stable ID = SHA-256("environment:" + project_key + ":" + key)[:16]
    """
    key: str = raw.get("key", "")
    name: str = raw.get("name", key or "Unnamed Environment")
    env_id: str = raw.get("_id", key)
    color: str = raw.get("color", "")
    default_ttl: int = raw.get("defaultTtl", 0)
    secure_mode: bool = raw.get("secureMode", False)
    default_track_events: bool = raw.get("defaultTrackEvents", False)
    tags: list[str] = raw.get("tags", [])

    proj_key = project_key or raw.get("projectKey", "")
    stable_key = f"{proj_key}:{key}" if proj_key else key
    source_id = _stable_id("environment", stable_key)

    content_parts = [
        f"Environment key: {key}",
        f"Name: {name}",
        f"Project: {proj_key}",
        f"Secure mode: {secure_mode}",
        f"Default track events: {default_track_events}",
    ]
    if color:
        content_parts.append(f"Color: #{color}")
    if default_ttl:
        content_parts.append(f"Default TTL: {default_ttl} min")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"LaunchDarkly environment: {name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{LD_APP_BASE}/{proj_key}/settings/environments",
        metadata={
            "env_id": env_id,
            "key": key,
            "name": name,
            "project_key": proj_key,
            "color": color,
            "default_ttl": default_ttl,
            "secure_mode": secure_mode,
            "default_track_events": default_track_events,
            "tags": tags,
        },
    )


def normalize_member(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a LaunchDarkly account member into a ConnectorDocument.

    Stable ID = SHA-256("member:" + _id)[:16]
    """
    member_id: str = raw.get("_id", "")
    email: str = raw.get("email", "")
    first_name: str = raw.get("firstName", "")
    last_name: str = raw.get("lastName", "")
    role: str = raw.get("role", "reader")
    verified: bool = raw.get("verified", False)
    creation_date: int = raw.get("creationDate", 0)
    last_seen: int = raw.get("lastSeen", 0)
    teams: list[dict[str, Any]] = raw.get("teams", [])

    display_name = f"{first_name} {last_name}".strip() or email or member_id
    source_id = _stable_id("member", member_id)

    content_parts = [
        f"Member ID: {member_id}",
        f"Email: {email}",
        f"Name: {display_name}",
        f"Role: {role}",
        f"Verified: {verified}",
    ]
    if creation_date:
        content_parts.append(f"Created: {creation_date}")
    if last_seen:
        content_parts.append(f"Last seen: {last_seen}")
    if teams:
        team_keys = [t.get("key", "") for t in teams if isinstance(t, dict)]
        content_parts.append(f"Teams: {', '.join(k for k in team_keys if k)}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"LaunchDarkly member: {display_name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{LD_APP_BASE}/settings/members",
        metadata={
            "member_id": member_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "role": role,
            "verified": verified,
            "creation_date": creation_date,
            "last_seen": last_seen,
            "team_count": len(teams),
        },
    )


def normalize_audit_entry(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a LaunchDarkly audit log entry into a ConnectorDocument.

    Stable ID = SHA-256("audit:" + _id)[:16]
    """
    entry_id: str = raw.get("_id", "")
    kind: str = raw.get("kind", "")
    name: str = raw.get("name", kind or "Audit entry")
    description: str = raw.get("description", "")
    date: int = raw.get("date", 0)
    comment: str = raw.get("comment", "")

    # Actor information
    member: dict[str, Any] = raw.get("member", {}) or {}
    actor_email: str = member.get("email", "")
    actor_name = f"{member.get('firstName', '')} {member.get('lastName', '')}".strip()

    # Target resource
    target: dict[str, Any] = raw.get("target", {}) or {}
    target_type: str = target.get("type", "")
    target_name: str = target.get("name", "")

    source_id = _stable_id("audit", entry_id)

    content_parts = [
        f"Entry ID: {entry_id}",
        f"Kind: {kind}",
        f"Name: {name}",
    ]
    if description:
        content_parts.append(f"Description: {description}")
    if actor_email:
        content_parts.append(f"Actor: {actor_name or actor_email} ({actor_email})")
    if target_type:
        content_parts.append(f"Target type: {target_type}")
    if target_name:
        content_parts.append(f"Target: {target_name}")
    if comment:
        content_parts.append(f"Comment: {comment}")
    if date:
        content_parts.append(f"Date: {date}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"LaunchDarkly audit: {name}",
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{LD_APP_BASE}/settings/audit-log",
        metadata={
            "entry_id": entry_id,
            "kind": kind,
            "name": name,
            "description": description,
            "date": date,
            "actor_email": actor_email,
            "actor_name": actor_name,
            "target_type": target_type,
            "target_name": target_name,
            "comment": comment,
        },
    )
