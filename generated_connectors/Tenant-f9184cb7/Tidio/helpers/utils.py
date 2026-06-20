from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import TidioAuthError, TidioError, TidioRateLimitError
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
    last_exc: TidioError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except TidioAuthError:
            raise
        except TidioRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except TidioError as exc:
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


def normalize_conversation(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Tidio conversation into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of "conversation:{id}" so it
    remains deterministic and collision-resistant.
    """
    conv_id: str = str(raw.get("id", ""))
    status: str = raw.get("status", "") or ""
    created_at: Any = raw.get("created_at", raw.get("createdAt", ""))
    updated_at: Any = raw.get("updated_at", raw.get("updatedAt", ""))
    visitor_id: str = str(raw.get("visitor_id", raw.get("visitorId", "")) or "")
    operator_id: str = str(raw.get("operator_id", raw.get("operatorId", "")) or "")
    unread_count: int = int(raw.get("unread_count", raw.get("unreadCount", 0)) or 0)

    display_id = conv_id or "unknown"
    title = f"Conversation: {display_id}"

    parts: list[str] = []
    parts.append(f"Conversation ID: {conv_id}")
    if status:
        parts.append(f"Status: {status}")
    if visitor_id:
        parts.append(f"Visitor ID: {visitor_id}")
    if operator_id:
        parts.append(f"Operator ID: {operator_id}")
    if unread_count:
        parts.append(f"Unread messages: {unread_count}")

    content = "\n".join(parts) if parts else title
    source_id = _short_hash(f"conversation:{conv_id}")
    source_url = f"https://www.tidio.com/panel/conversations/{conv_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "conversation_id": conv_id,
            "status": status,
            "visitor_id": visitor_id,
            "operator_id": operator_id,
            "unread_count": unread_count,
            "created_at": created_at,
            "updated_at": updated_at,
            "type": "conversation",
        },
    )


def normalize_visitor(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Tidio visitor into a ConnectorDocument."""
    visitor_id: str = str(raw.get("id", ""))
    email: str = raw.get("email", "") or ""
    name: str = raw.get("name", "") or ""
    ip: str = raw.get("ip", "") or ""
    country: str = raw.get("country", "") or ""
    city: str = raw.get("city", "") or ""
    created_at: Any = raw.get("created_at", raw.get("createdAt", ""))

    display_name = name or email or f"Visitor {visitor_id}"
    title = f"Visitor: {display_name}"

    parts: list[str] = []
    if name:
        parts.append(f"Name: {name}")
    if email:
        parts.append(f"Email: {email}")
    if ip:
        parts.append(f"IP: {ip}")
    if country:
        parts.append(f"Country: {country}")
    if city:
        parts.append(f"City: {city}")

    content = "\n".join(parts) if parts else display_name
    source_id = _short_hash(f"visitor:{visitor_id}")
    source_url = f"https://www.tidio.com/panel/visitors/{visitor_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "visitor_id": visitor_id,
            "email": email,
            "name": name,
            "ip": ip,
            "country": country,
            "city": city,
            "created_at": created_at,
            "type": "visitor",
        },
    )


def normalize_chatbot(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Tidio chatbot into a ConnectorDocument."""
    chatbot_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "") or ""
    status: str = raw.get("status", "") or ""
    created_at: Any = raw.get("created_at", raw.get("createdAt", ""))
    updated_at: Any = raw.get("updated_at", raw.get("updatedAt", ""))

    display_name = name or f"Chatbot {chatbot_id}"
    title = f"Chatbot: {display_name}"

    parts: list[str] = []
    if name:
        parts.append(f"Name: {name}")
    if status:
        parts.append(f"Status: {status}")
    parts.append(f"Chatbot ID: {chatbot_id}")

    content = "\n".join(parts) if parts else display_name
    source_id = _short_hash(f"chatbot:{chatbot_id}")
    source_url = f"https://www.tidio.com/panel/chatbots/{chatbot_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "chatbot_id": chatbot_id,
            "name": name,
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
            "type": "chatbot",
        },
    )
