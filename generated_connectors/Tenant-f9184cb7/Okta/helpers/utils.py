from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import OktaAuthError, OktaError, OktaRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _stable_id(entity_type: str, entity_id: str) -> str:
    """SHA-256(entity_type + ':' + entity_id)[:16] — stable, collision-resistant source ID."""
    raw = f"{entity_type}:{entity_id}"
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
    Rate-limit errors respect retry_after when available.
    """
    last_exc: OktaError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except OktaAuthError:
            raise
        except OktaRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except OktaError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_user(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Okta user object into a ConnectorDocument.

    Stable source_id = sha256('user:' + okta_user_id)[:16].
    """
    user_id: str = raw.get("id", "") or ""
    source_id = _stable_id("user", user_id)

    profile: dict[str, Any] = raw.get("profile", {}) or {}
    first_name: str = profile.get("firstName", "") or ""
    last_name: str = profile.get("lastName", "") or ""
    email: str = profile.get("email", "") or ""
    login: str = profile.get("login", "") or ""
    display_name: str = f"{first_name} {last_name}".strip() or login or email
    status: str = raw.get("status", "") or ""
    created: str = raw.get("created", "") or ""
    activated: str = raw.get("activated", "") or ""
    last_login: str = raw.get("lastLogin", "") or ""
    last_updated: str = raw.get("lastUpdated", "") or ""
    dept: str = profile.get("department", "") or ""
    title: str = profile.get("title", "") or ""
    mobile_phone: str = profile.get("mobilePhone", "") or ""
    org: str = profile.get("organization", "") or ""

    content_parts = [
        f"Okta User: {display_name}",
        f"Email: {email}" if email else "",
        f"Login: {login}" if login else "",
        f"Status: {status}" if status else "",
        f"Department: {dept}" if dept else "",
        f"Title: {title}" if title else "",
        f"Organization: {org}" if org else "",
        f"Mobile: {mobile_phone}" if mobile_phone else "",
        f"Created: {created}" if created else "",
        f"Activated: {activated}" if activated else "",
        f"Last Login: {last_login}" if last_login else "",
        f"Last Updated: {last_updated}" if last_updated else "",
        f"ID: {user_id}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"Okta User: {display_name}",
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "user",
            "okta_id": user_id,
            "email": email,
            "login": login,
            "display_name": display_name,
            "first_name": first_name,
            "last_name": last_name,
            "status": status,
            "department": dept,
            "title": title,
            "organization": org,
            "created": created,
            "activated": activated,
            "last_login": last_login,
            "last_updated": last_updated,
        },
    )


def normalize_group(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Okta group object into a ConnectorDocument.

    Stable source_id = sha256('group:' + okta_group_id)[:16].
    """
    group_id: str = raw.get("id", "") or ""
    source_id = _stable_id("group", group_id)

    profile: dict[str, Any] = raw.get("profile", {}) or {}
    name: str = profile.get("name", "") or f"Group {group_id}"
    description: str = profile.get("description", "") or ""
    group_type: str = raw.get("type", "") or ""
    created: str = raw.get("created", "") or ""
    last_updated: str = raw.get("lastUpdated", "") or ""
    last_membership_updated: str = raw.get("lastMembershipUpdated", "") or ""
    object_class: list[str] = raw.get("objectClass", []) or []

    content_parts = [
        f"Okta Group: {name}",
        f"Description: {description}" if description else "",
        f"Type: {group_type}" if group_type else "",
        f"Object Class: {', '.join(object_class)}" if object_class else "",
        f"Created: {created}" if created else "",
        f"Last Updated: {last_updated}" if last_updated else "",
        f"Last Membership Updated: {last_membership_updated}" if last_membership_updated else "",
        f"ID: {group_id}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"Okta Group: {name}",
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "group",
            "okta_id": group_id,
            "name": name,
            "description": description,
            "type": group_type,
            "object_class": object_class,
            "created": created,
            "last_updated": last_updated,
            "last_membership_updated": last_membership_updated,
        },
    )


def normalize_app(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Okta application object into a ConnectorDocument.

    Stable source_id = sha256('app:' + okta_app_id)[:16].
    """
    app_id: str = raw.get("id", "") or ""
    source_id = _stable_id("app", app_id)

    label: str = raw.get("label", "") or f"App {app_id}"
    name: str = raw.get("name", "") or ""
    status: str = raw.get("status", "") or ""
    sign_on_mode: str = raw.get("signOnMode", "") or ""
    created: str = raw.get("created", "") or ""
    last_updated: str = raw.get("lastUpdated", "") or ""
    features: list[str] = raw.get("features", []) or []

    # Accessibility / visibility
    accessibility: dict[str, Any] = raw.get("accessibility", {}) or {}
    self_service: bool = accessibility.get("selfService", False)

    content_parts = [
        f"Okta Application: {label}",
        f"Name: {name}" if name else "",
        f"Status: {status}" if status else "",
        f"Sign-on Mode: {sign_on_mode}" if sign_on_mode else "",
        f"Features: {', '.join(features)}" if features else "",
        f"Self-service: {self_service}",
        f"Created: {created}" if created else "",
        f"Last Updated: {last_updated}" if last_updated else "",
        f"ID: {app_id}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"Okta App: {label}",
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "app",
            "okta_id": app_id,
            "label": label,
            "name": name,
            "status": status,
            "sign_on_mode": sign_on_mode,
            "features": features,
            "self_service": self_service,
            "created": created,
            "last_updated": last_updated,
        },
    )


def normalize_log(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Okta system log event into a ConnectorDocument.

    Stable source_id = sha256('log:' + uuid)[:16].
    """
    uuid: str = raw.get("uuid", "") or ""
    source_id = _stable_id("log", uuid)

    event_type: str = raw.get("eventType", "") or ""
    display_message: str = raw.get("displayMessage", "") or ""
    severity: str = raw.get("severity", "") or ""
    published: str = raw.get("published", "") or ""
    outcome: dict[str, Any] = raw.get("outcome", {}) or {}
    outcome_result: str = outcome.get("result", "") or ""
    outcome_reason: str = outcome.get("reason", "") or ""

    actor: dict[str, Any] = raw.get("actor", {}) or {}
    actor_display: str = actor.get("displayName", "") or actor.get("id", "") or ""
    actor_type: str = actor.get("type", "") or ""

    client: dict[str, Any] = raw.get("client", {}) or {}
    ip_address: str = client.get("ipAddress", "") or ""
    user_agent_raw: dict[str, Any] = client.get("userAgent", {}) or {}
    user_agent: str = user_agent_raw.get("rawUserAgent", "") or ""

    target_list: list[dict[str, Any]] = raw.get("target", []) or []
    targets: list[str] = [
        t.get("displayName", "") or t.get("id", "")
        for t in target_list
        if t.get("displayName") or t.get("id")
    ]

    content_parts = [
        f"Okta Log Event: {event_type}",
        f"Message: {display_message}" if display_message else "",
        f"Severity: {severity}" if severity else "",
        f"Published: {published}" if published else "",
        f"Actor: {actor_display} ({actor_type})" if actor_display else "",
        f"Outcome: {outcome_result}" if outcome_result else "",
        f"Reason: {outcome_reason}" if outcome_reason else "",
        f"IP: {ip_address}" if ip_address else "",
        f"User Agent: {user_agent}" if user_agent else "",
        f"Targets: {', '.join(targets)}" if targets else "",
        f"UUID: {uuid}" if uuid else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"Okta Log: {event_type} — {display_message}",
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "log",
            "uuid": uuid,
            "event_type": event_type,
            "display_message": display_message,
            "severity": severity,
            "published": published,
            "outcome_result": outcome_result,
            "outcome_reason": outcome_reason,
            "actor": actor_display,
            "actor_type": actor_type,
            "ip_address": ip_address,
            "targets": targets,
        },
    )
