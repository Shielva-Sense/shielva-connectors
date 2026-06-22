"""Microsoft Teams connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, Optional

from models import ConnectorDocument


def normalize_message(
    msg: Dict[str, Any],
    team_id: str,
    channel_id: str,
) -> ConnectorDocument:
    """Convert a Microsoft Graph Teams message object into a ConnectorDocument.

    The stable document id is SHA-256(team_id + ':' + channel_id + ':' + message_id)[:16].
    """
    message_id: str = msg.get("id", "")
    stable_id = hashlib.sha256(
        f"{team_id}:{channel_id}:{message_id}".encode()
    ).hexdigest()[:16]

    # Extract sender info
    sender_name: str = ""
    sender_email: str = ""
    from_info = msg.get("from") or {}
    user_info = from_info.get("user") or {}
    application_info = from_info.get("application") or {}

    if user_info:
        sender_name = user_info.get("displayName", "")
        sender_email = user_info.get("id", "")
    elif application_info:
        sender_name = application_info.get("displayName", "Application")

    # Extract body / text content
    body_info = msg.get("body") or {}
    body_content: str = body_info.get("content", "") or ""
    body_type: str = body_info.get("contentType", "text") or "text"

    # Strip HTML tags for html-type messages (basic stripping)
    if body_type == "html":
        import re
        body_content = re.sub(r"<[^>]+>", "", body_content).strip()

    # Timestamps
    created_at: str = msg.get("createdDateTime", "") or ""
    modified_at: str = msg.get("lastModifiedDateTime", "") or ""

    # Build a human-readable title
    title_parts = []
    if team_id:
        title_parts.append(f"Team:{team_id[:8]}")
    if channel_id:
        title_parts.append(f"Channel:{channel_id[:8]}")
    if sender_name:
        title_parts.append(f"@{sender_name}")
    title = " — ".join(title_parts) if title_parts else f"Teams message {message_id}"

    # Build content
    content_parts: list[str] = []
    if team_id:
        content_parts.append(f"Team: {team_id}")
    if channel_id:
        content_parts.append(f"Channel: {channel_id}")
    if sender_name:
        content_parts.append(f"From: {sender_name}")
    if created_at:
        content_parts.append(f"Sent: {created_at}")
    if body_content:
        content_parts.append(f"Message: {body_content}")

    # Attachments summary
    attachments = msg.get("attachments") or []
    if attachments:
        names = [a.get("name", "") for a in attachments if a.get("name")]
        if names:
            content_parts.append(f"Attachments: {', '.join(names)}")

    metadata: Dict[str, Any] = {
        "team_id": team_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "sender_name": sender_name,
        "sender_id": sender_email,
        "created_at": created_at,
        "modified_at": modified_at,
        "body_type": body_type,
        "message_type": msg.get("messageType", "message"),
        "importance": msg.get("importance", "normal"),
        "reply_to_id": msg.get("replyToId"),
        "attachments_count": len(attachments),
        "source": "microsoft_teams",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="teams_message",
        metadata=metadata,
    )


async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on MicrosoftTeamsAuthError — re-authorizing is required.
    Retries on network errors and rate-limit errors.
    """
    from exceptions import MicrosoftTeamsAuthError, MicrosoftTeamsError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except MicrosoftTeamsAuthError:
            raise
        except MicrosoftTeamsError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
