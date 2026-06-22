"""Pydantic schemas for the Telegram Bot API surface.

These mirror the subset of the Telegram Bot API the connector exposes. The
connector boundary itself uses raw ``Dict[str, Any]`` payloads (Telegram's
wire format is plain snake-case JSON, no aliases); these models exist to
document the response shape and ease typed deserialisation in callers that
want it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _TelegramModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class TelegramUser(_TelegramModel):
    """A Telegram User object (the bot itself, or a sender)."""

    id: int
    is_bot: bool
    first_name: str
    username: Optional[str] = None
    last_name: Optional[str] = None
    language_code: Optional[str] = None
    can_join_groups: Optional[bool] = None
    can_read_all_group_messages: Optional[bool] = None
    supports_inline_queries: Optional[bool] = None


class TelegramChat(_TelegramModel):
    """Subset of the Telegram Chat object used by the connector."""

    id: int
    type: str
    title: Optional[str] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    description: Optional[str] = None


class TelegramMessage(_TelegramModel):
    """Subset of the Telegram Message object."""

    message_id: int
    chat: Dict[str, Any]
    date: int
    text: Optional[str] = None
    caption: Optional[str] = None
    from_user: Optional[Dict[str, Any]] = Field(default=None, alias="from")
    reply_to_message: Optional[Dict[str, Any]] = None
    entities: List[Dict[str, Any]] = Field(default_factory=list)


class TelegramUpdate(_TelegramModel):
    """A single Update object as returned by getUpdates / webhook POST."""

    update_id: int
    message: Optional[Dict[str, Any]] = None
    edited_message: Optional[Dict[str, Any]] = None
    channel_post: Optional[Dict[str, Any]] = None
    edited_channel_post: Optional[Dict[str, Any]] = None
    callback_query: Optional[Dict[str, Any]] = None
    inline_query: Optional[Dict[str, Any]] = None


class TelegramWebhookInfo(_TelegramModel):
    """Result of /getWebhookInfo."""

    url: str
    has_custom_certificate: bool = False
    pending_update_count: int = 0
    ip_address: Optional[str] = None
    last_error_date: Optional[int] = None
    last_error_message: Optional[str] = None
    max_connections: Optional[int] = None
    allowed_updates: List[str] = Field(default_factory=list)


class TelegramFile(_TelegramModel):
    """Result of /getFile — caller appends file_path to /file/bot{token}/."""

    file_id: str
    file_unique_id: str
    file_size: Optional[int] = None
    file_path: Optional[str] = None
