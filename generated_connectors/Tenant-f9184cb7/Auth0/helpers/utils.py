"""Auth0 connector helpers — normalizers and retry logic."""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import Auth0AuthError, Auth0Error, Auth0RateLimitError
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

    Auth errors are NOT retried — they require human intervention (credential fix).
    Rate-limit errors respect retry_after when available.
    """
    last_exc: Auth0Error | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except Auth0AuthError:
            raise
        except Auth0RateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except Auth0Error as exc:
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
    """Convert a raw Auth0 user object into a ConnectorDocument.

    Stable source_id = sha256('user:' + user_id)[:16].
    """
    user_id: str = raw.get("user_id", "") or ""
    source_id = _stable_id("user", user_id)

    name: str = raw.get("name", "") or ""
    email: str = raw.get("email", "") or ""
    nickname: str = raw.get("nickname", "") or ""
    picture: str = raw.get("picture", "") or ""
    email_verified: bool = bool(raw.get("email_verified", False))
    blocked: bool = bool(raw.get("blocked", False))
    created_at: str = raw.get("created_at", "") or ""
    updated_at: str = raw.get("updated_at", "") or ""
    last_login: str = raw.get("last_login", "") or ""
    last_ip: str = raw.get("last_ip", "") or ""
    logins_count: int = int(raw.get("logins_count", 0) or 0)
    connection: str = (
        raw.get("identities", [{}])[0].get("connection", "")
        if raw.get("identities")
        else ""
    )

    display_name = name or nickname or email or user_id
    status = "blocked" if blocked else ("unverified" if not email_verified else "active")

    content_parts = [
        f"Auth0 User: {display_name}",
        f"Email: {email}" if email else "",
        f"Nickname: {nickname}" if nickname else "",
        f"Status: {status}",
        f"Email Verified: {email_verified}",
        f"Blocked: {blocked}",
        f"Connection: {connection}" if connection else "",
        f"Logins Count: {logins_count}",
        f"Last Login: {last_login}" if last_login else "",
        f"Last IP: {last_ip}" if last_ip else "",
        f"Created: {created_at}" if created_at else "",
        f"Updated: {updated_at}" if updated_at else "",
        f"ID: {user_id}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"Auth0 User: {display_name}",
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "user",
            "auth0_id": user_id,
            "email": email,
            "name": name,
            "nickname": nickname,
            "picture": picture,
            "email_verified": email_verified,
            "blocked": blocked,
            "status": status,
            "connection": connection,
            "logins_count": logins_count,
            "last_login": last_login,
            "last_ip": last_ip,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_role(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Auth0 role object into a ConnectorDocument.

    Stable source_id = sha256('role:' + role_id)[:16].
    """
    role_id: str = raw.get("id", "") or ""
    source_id = _stable_id("role", role_id)

    name: str = raw.get("name", "") or f"Role {role_id}"
    description: str = raw.get("description", "") or ""

    content_parts = [
        f"Auth0 Role: {name}",
        f"Description: {description}" if description else "",
        f"ID: {role_id}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"Auth0 Role: {name}",
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "role",
            "auth0_id": role_id,
            "name": name,
            "description": description,
        },
    )


def normalize_client(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Auth0 client (application) object into a ConnectorDocument.

    Stable source_id = sha256('client:' + client_id)[:16].
    """
    client_id: str = raw.get("client_id", "") or ""
    source_id = _stable_id("client", client_id)

    name: str = raw.get("name", "") or f"Client {client_id}"
    description: str = raw.get("description", "") or ""
    app_type: str = raw.get("app_type", "") or ""
    callbacks: list[str] = raw.get("callbacks", []) or []
    allowed_origins: list[str] = raw.get("allowed_origins", []) or []
    web_origins: list[str] = raw.get("web_origins", []) or []
    tenant_name: str = raw.get("tenant", "") or ""
    logo_uri: str = raw.get("logo_uri", "") or ""
    is_first_party: bool = bool(raw.get("is_first_party", False))
    oidc_conformant: bool = bool(raw.get("oidc_conformant", False))

    content_parts = [
        f"Auth0 Client: {name}",
        f"Description: {description}" if description else "",
        f"App Type: {app_type}" if app_type else "",
        f"Tenant: {tenant_name}" if tenant_name else "",
        f"First Party: {is_first_party}",
        f"OIDC Conformant: {oidc_conformant}",
        f"Callbacks: {', '.join(callbacks)}" if callbacks else "",
        f"Allowed Origins: {', '.join(allowed_origins)}" if allowed_origins else "",
        f"Web Origins: {', '.join(web_origins)}" if web_origins else "",
        f"Client ID: {client_id}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"Auth0 Client: {name}",
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "client",
            "auth0_id": client_id,
            "name": name,
            "description": description,
            "app_type": app_type,
            "tenant": tenant_name,
            "logo_uri": logo_uri,
            "is_first_party": is_first_party,
            "oidc_conformant": oidc_conformant,
            "callbacks": callbacks,
            "allowed_origins": allowed_origins,
            "web_origins": web_origins,
        },
    )


def normalize_connection(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Auth0 connection object into a ConnectorDocument.

    Stable source_id = sha256('connection:' + connection_id)[:16].
    """
    connection_id: str = raw.get("id", "") or ""
    source_id = _stable_id("connection", connection_id)

    name: str = raw.get("name", "") or f"Connection {connection_id}"
    strategy: str = raw.get("strategy", "") or ""
    display_name: str = raw.get("display_name", "") or name
    enabled_clients: list[str] = raw.get("enabled_clients", []) or []
    realms: list[str] = raw.get("realms", []) or []
    is_domain_connection: bool = bool(raw.get("is_domain_connection", False))
    metadata: dict[str, Any] = raw.get("metadata", {}) or {}

    content_parts = [
        f"Auth0 Connection: {display_name}",
        f"Strategy: {strategy}" if strategy else "",
        f"Domain Connection: {is_domain_connection}",
        f"Enabled Clients: {', '.join(enabled_clients)}" if enabled_clients else "",
        f"Realms: {', '.join(realms)}" if realms else "",
        f"ID: {connection_id}",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"Auth0 Connection: {display_name}",
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "connection",
            "auth0_id": connection_id,
            "name": name,
            "display_name": display_name,
            "strategy": strategy,
            "is_domain_connection": is_domain_connection,
            "enabled_clients": enabled_clients,
            "realms": realms,
            "connection_metadata": metadata,
        },
    )


def normalize_log(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Auth0 log event into a ConnectorDocument.

    Stable source_id = sha256('log:' + log_id)[:16].
    """
    log_id: str = raw.get("_id", "") or raw.get("id", "") or ""
    source_id = _stable_id("log", log_id)

    event_type: str = raw.get("type", "") or ""
    description: str = raw.get("description", "") or ""
    date: str = raw.get("date", "") or ""
    ip: str = raw.get("ip", "") or ""
    user_agent: str = raw.get("user_agent", "") or ""
    user_name: str = raw.get("user_name", "") or ""
    user_id: str = raw.get("user_id", "") or ""
    connection: str = raw.get("connection", "") or ""
    connection_id: str = raw.get("connection_id", "") or ""
    client_id: str = raw.get("client_id", "") or ""
    client_name: str = raw.get("client_name", "") or ""
    log_audience: str = raw.get("audience", "") or ""
    scope: list[str] | str = raw.get("scope", []) or []
    if isinstance(scope, list):
        scope_str = " ".join(scope)
    else:
        scope_str = str(scope)

    content_parts = [
        f"Auth0 Log Event: {event_type}",
        f"Description: {description}" if description else "",
        f"Date: {date}" if date else "",
        f"User: {user_name}" if user_name else "",
        f"User ID: {user_id}" if user_id else "",
        f"IP: {ip}" if ip else "",
        f"User Agent: {user_agent}" if user_agent else "",
        f"Connection: {connection}" if connection else "",
        f"Client: {client_name}" if client_name else "",
        f"Audience: {log_audience}" if log_audience else "",
        f"Scope: {scope_str}" if scope_str else "",
        f"Log ID: {log_id}" if log_id else "",
    ]

    return ConnectorDocument(
        source_id=source_id,
        title=f"Auth0 Log: {event_type}" + (f" — {description[:60]}" if description else ""),
        content="\n".join(p for p in content_parts if p),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "entity_type": "log",
            "log_id": log_id,
            "type": event_type,
            "description": description,
            "date": date,
            "ip": ip,
            "user_agent": user_agent,
            "user_name": user_name,
            "user_id": user_id,
            "connection": connection,
            "connection_id": connection_id,
            "client_id": client_id,
            "client_name": client_name,
            "audience": log_audience,
        },
    )
