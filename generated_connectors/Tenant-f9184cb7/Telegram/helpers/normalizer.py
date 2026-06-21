"""Transforms raw Telegram Bot API responses into NormalizedDocument objects.

SOC: pure functions only — no HTTP, no logging side-effects, no env reads.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.base_connector import NormalizedDocument


def _format_user(user: Optional[Dict[str, Any]]) -> str:
    """Render a Telegram User dict as ``@username`` or ``First Last``."""
    if not user:
        return ""
    if user.get("username"):
        return f"@{user['username']}"
    first = user.get("first_name") or ""
    last = user.get("last_name") or ""
    full = f"{first} {last}".strip()
    return full or str(user.get("id", ""))


def _format_chat_title(chat: Dict[str, Any]) -> str:
    """Pick the best display label for a Telegram Chat (group title, username, or first name)."""
    return (
        chat.get("title")
        or chat.get("username")
        or chat.get("first_name")
        or str(chat.get("id", ""))
    )


def normalize_message(
    message: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert a Telegram Message dict to a :class:`NormalizedDocument`.

    Accepts the inner ``message`` object from both ``getUpdates`` payloads
    (``update["message"]``) and direct Message responses from
    ``sendMessage`` / ``forwardMessage``.

    The document ``id`` is tenant-scoped via ``connector_id``
    (``f"{connector_id}_{chat_id}_{message_id}"``) so two different tenants
    that observe the same message in their own bots produce distinct rows.
    """
    message_id = message.get("message_id")
    chat = message.get("chat") or {}
    from_user = message.get("from") or {}
    date_ts = int(message.get("date") or 0)

    created_at: Optional[datetime] = None
    if date_ts:
        created_at = datetime.fromtimestamp(date_ts, tz=timezone.utc)

    text = message.get("text") or message.get("caption") or ""
    author = _format_user(from_user)
    chat_title = _format_chat_title(chat)
    chat_id = chat.get("id")

    return NormalizedDocument(
        id=f"{connector_id}_{chat_id}_{message_id}",
        source_id=str(message_id),
        title=f"Telegram message in {chat_title}",
        content=text,
        content_type="text",
        source_url=None,
        author=author,
        created_at=created_at,
        updated_at=created_at,
        source="telegram",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "chat_id": chat_id,
            "chat_type": chat.get("type"),
            "chat_title": chat_title,
            "from_user_id": from_user.get("id"),
            "from_username": from_user.get("username"),
            "entities": message.get("entities") or [],
            "reply_to_message_id": (message.get("reply_to_message") or {}).get(
                "message_id"
            ),
            "kind": "telegram.message",
        },
    )
