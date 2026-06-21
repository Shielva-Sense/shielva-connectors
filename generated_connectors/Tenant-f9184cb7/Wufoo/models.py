"""Pydantic request/response schemas for the Wufoo REST API (v3).

PascalCase aliases match Wufoo's wire format (``Hash``, ``EntryId``,
``DateCreated``); the connector boundary uses Dict[str, Any] payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _WufooModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_WufooModel):
    """Wufoo entries pagination params."""
    page_start: int = Field(default=0, alias="pageStart")
    page_size: int = Field(default=25, alias="pageSize")
    sort: Optional[str] = None
    sort_direction: str = Field(default="DESC", alias="sortDirection")


class WufooForm(_WufooModel):
    """A Wufoo form summary as returned by /forms.json."""
    hash: str = Field(alias="Hash")
    name: str = Field(alias="Name")
    description: Optional[str] = Field(default=None, alias="Description")
    url: Optional[str] = Field(default=None, alias="Url")
    entry_count: Optional[int] = Field(default=None, alias="EntryCount")
    is_public: Optional[bool] = Field(default=None, alias="IsPublic")
    date_created: Optional[datetime] = Field(default=None, alias="DateCreated")
    date_updated: Optional[datetime] = Field(default=None, alias="DateUpdated")


class WufooField(_WufooModel):
    """A Wufoo form field definition as returned by /forms/{id}/fields.json."""
    field_id: str = Field(alias="ID")
    title: str = Field(alias="Title")
    type: str = Field(alias="Type")
    is_required: Optional[bool] = Field(default=None, alias="IsRequired")
    default_val: Optional[str] = Field(default=None, alias="DefaultVal")
    sub_fields: List[Dict[str, Any]] = Field(default_factory=list, alias="SubFields")


class WufooEntry(_WufooModel):
    """A single submitted entry — flexible payload of FieldN -> value."""
    entry_id: str = Field(alias="EntryId")
    date_created: Optional[datetime] = Field(default=None, alias="DateCreated")
    date_updated: Optional[datetime] = Field(default=None, alias="DateUpdated")
    created_by: Optional[str] = Field(default=None, alias="CreatedBy")


class WufooSubmitResult(_WufooModel):
    """Response from POST /forms/{id}/entries.json."""
    success: int = Field(alias="Success")
    entry_id: Optional[str] = Field(default=None, alias="EntryId")
    error_text: Optional[str] = Field(default=None, alias="ErrorText")
    field_errors: List[Dict[str, Any]] = Field(default_factory=list, alias="FieldErrors")


class WufooWebhook(_WufooModel):
    """Webhook registered against a form."""
    hash: Optional[str] = Field(default=None, alias="Hash")
    url: str
    handshake_key: Optional[str] = Field(default=None, alias="handshakeKey")
    metadata: Optional[bool] = None


class WufooComment(_WufooModel):
    """A single comment on a form entry."""
    comment_id: Optional[str] = Field(default=None, alias="CommentId")
    entry_id: Optional[str] = Field(default=None, alias="EntryId")
    text: Optional[str] = Field(default=None, alias="Text")
    commenter_name: Optional[str] = Field(default=None, alias="CommenterName")
    date_created: Optional[datetime] = Field(default=None, alias="DateCreated")


class WufooReport(_WufooModel):
    """A Wufoo report metadata object."""
    hash: str = Field(alias="Hash")
    name: str = Field(alias="Name")
    description: Optional[str] = Field(default=None, alias="Description")
    is_public: Optional[bool] = Field(default=None, alias="IsPublic")
    date_created: Optional[datetime] = Field(default=None, alias="DateCreated")
    date_updated: Optional[datetime] = Field(default=None, alias="DateUpdated")
