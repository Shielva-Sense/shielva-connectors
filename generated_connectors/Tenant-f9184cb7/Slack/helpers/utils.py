"""Slack connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, Optional

from models import ConnectorDocument


def normalize_message(
    message: Dict[str, Any],
    channel_id: str,
    channel_name: str,
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Slack message object into a ConnectorDocument.

    The stable document id is sha256(channel_id + message_ts)[:16].
    """
    ts = message.get("ts", "")
    stable_id = hashlib.sha256(
        f"{channel_id}{ts}".encode()
    ).hexdigest()[:16]

    user = message.get("user", "")
    text = message.get("text", "") or ""
    thread_ts = message.get("thread_ts")
    subtype = message.get("subtype", "")

    # Build a human-readable title
    title_parts = []
    if channel_name:
        title_parts.append(f"#{channel_name}")
    if user:
        title_parts.append(f"@{user}")
    title = " — ".join(title_parts) if title_parts else f"Slack message {ts}"

    # Build content
    content_parts: list[str] = []
    if channel_name:
        content_parts.append(f"Channel: #{channel_name}")
    if user:
        content_parts.append(f"User: {user}")
    content_parts.append(f"Timestamp: {ts}")
    if thread_ts and thread_ts != ts:
        content_parts.append(f"Thread: {thread_ts}")
    if text:
        content_parts.append(f"Message: {text}")

    metadata: Dict[str, Any] = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "ts": ts,
        "user": user,
        "thread_ts": thread_ts,
        "subtype": subtype,
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "slack",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="slack_message",
        metadata=metadata,
    )


async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on SlackAuthError — re-authorizing is required.
    """
    from exceptions import SlackAuthError, SlackError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except SlackAuthError:
            raise
        except SlackError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
