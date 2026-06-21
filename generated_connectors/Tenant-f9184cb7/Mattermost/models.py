"""Pydantic request/response schemas for Mattermost REST API v4.

Mattermost uses **snake_case** on the wire; the connector boundary still
exposes ``Dict[str, Any]`` payloads, but these schemas provide typed envelopes
for internal validation and documentation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _MMModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_MMModel):
    page: int = 0
    per_page: int = 60


class UserResponse(_MMModel):
    id: str
    username: Optional[str] = None
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    roles: Optional[str] = None
    locale: Optional[str] = None
    create_at: Optional[int] = None
    update_at: Optional[int] = None


class TeamResponse(_MMModel):
    id: str
    name: Optional[str] = None
    display_name: Optional[str] = None
    type: Optional[str] = None
    create_at: Optional[int] = None


class ChannelResponse(_MMModel):
    id: str
    team_id: Optional[str] = None
    name: Optional[str] = None
    display_name: Optional[str] = None
    type: Optional[str] = None
    purpose: Optional[str] = None
    header: Optional[str] = None
    create_at: Optional[int] = None
    update_at: Optional[int] = None
    delete_at: Optional[int] = None


class CreateChannelRequest(_MMModel):
    team_id: str
    name: str
    display_name: str
    type: str = "O"  # "O" = public, "P" = private
    purpose: str = ""
    header: str = ""


class PostResponse(_MMModel):
    id: str
    channel_id: Optional[str] = None
    user_id: Optional[str] = None
    root_id: Optional[str] = None
    message: Optional[str] = None
    type: Optional[str] = None
    props: Dict[str, Any] = Field(default_factory=dict)
    file_ids: List[str] = Field(default_factory=list)
    create_at: Optional[int] = None
    update_at: Optional[int] = None
    edit_at: Optional[int] = None


class CreatePostRequest(_MMModel):
    channel_id: str
    message: str
    root_id: Optional[str] = None
    props: Optional[Dict[str, Any]] = None
    file_ids: Optional[List[str]] = None


class IncomingWebhookRequest(_MMModel):
    channel_id: str
    display_name: str
    description: str = ""
    username: Optional[str] = None
    icon_url: Optional[str] = None


class OutgoingWebhookRequest(_MMModel):
    team_id: str
    display_name: str
    trigger_words: List[str] = Field(default_factory=list)
    callback_urls: List[str] = Field(default_factory=list)
    channel_id: Optional[str] = None
    description: str = ""
    content_type: str = "application/x-www-form-urlencoded"


class PageResult(_MMModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_cursor: Optional[str] = None
