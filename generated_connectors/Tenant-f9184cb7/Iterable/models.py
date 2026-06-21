"""Pydantic request/response schemas for the Iterable REST API.

camelCase aliases match Iterable's wire format; the connector boundary uses
plain `Dict[str, Any]` payloads so callers don't need to know about pydantic.

Legacy dataclass-based shims (`IterableUser`, `IterableEvent`, etc.) are
kept for back-compat with older test fixtures that may import them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _IterableModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class UserIdentity(_IterableModel):
    """Either email or userId is required by every Iterable user-scoped call."""
    email: Optional[str] = None
    user_id: Optional[str] = Field(default=None, alias="userId")


class UpdateUserRequest(UserIdentity):
    data_fields: Dict[str, Any] = Field(default_factory=dict, alias="dataFields")
    merge_nested_objects: bool = Field(default=True, alias="mergeNestedObjects")


class TrackEventRequest(_IterableModel):
    email: str
    event_name: str = Field(alias="eventName")
    data_fields: Dict[str, Any] = Field(default_factory=dict, alias="dataFields")
    campaign_id: Optional[int] = Field(default=None, alias="campaignId")
    template_id: Optional[int] = Field(default=None, alias="templateId")
    id: Optional[str] = None
    created_at: Optional[int] = Field(default=None, alias="createdAt")


class SendEmailRequest(_IterableModel):
    campaign_id: int = Field(alias="campaignId")
    recipient_email: str = Field(alias="recipientEmail")
    data_fields: Dict[str, Any] = Field(default_factory=dict, alias="dataFields")
    send_at: Optional[str] = Field(default=None, alias="sendAt")
    metadata: Optional[Dict[str, Any]] = None


class SubscribeRequest(_IterableModel):
    list_id: int = Field(alias="listId")
    subscribers: List[Dict[str, Any]] = Field(default_factory=list)


class ListResponse(_IterableModel):
    list_id: int = Field(alias="id")
    name: Optional[str] = None
    created_at: Optional[int] = Field(default=None, alias="createdAt")
    list_type: Optional[str] = Field(default=None, alias="listType")
    description: Optional[str] = None


class TemplateResponse(_IterableModel):
    template_id: int = Field(alias="templateId")
    name: Optional[str] = None
    created_at: Optional[int] = Field(default=None, alias="createdAt")
    updated_at: Optional[int] = Field(default=None, alias="updatedAt")
    message_medium: Optional[str] = Field(default=None, alias="messageMedium")
    template_type: Optional[str] = Field(default=None, alias="templateType")
    campaign_id: Optional[int] = Field(default=None, alias="campaignId")
    html: Optional[str] = None
    plain_text: Optional[str] = Field(default=None, alias="plainText")


class CampaignResponse(_IterableModel):
    campaign_id: int = Field(alias="id")
    name: Optional[str] = None
    template_id: Optional[int] = Field(default=None, alias="templateId")
    message_medium: Optional[str] = Field(default=None, alias="messageMedium")
    campaign_state: Optional[str] = Field(default=None, alias="campaignState")
    created_at: Optional[int] = Field(default=None, alias="createdAt")
    list_ids: List[int] = Field(default_factory=list, alias="listIds")


# ── Legacy dataclass shims ────────────────────────────────────────────────


@dataclass
class IterableUser:
    """Iterable user identity — either email or userId is required."""
    email: Optional[str] = None
    user_id: Optional[str] = None
    data_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IterableEvent:
    """A single custom event for /events/track."""
    email: str
    event_name: str
    data_fields: Dict[str, Any] = field(default_factory=dict)
    campaign_id: Optional[int] = None
    template_id: Optional[int] = None


@dataclass
class IterableList:
    """An Iterable list (audience segment)."""
    id: int
    name: str
    created_at: Optional[int] = None
    list_type: Optional[str] = None


@dataclass
class IterableTemplate:
    """An Iterable message template (email/push/sms/in-app)."""
    template_id: int
    name: Optional[str] = None
    created_at: Optional[int] = None
    message_medium: Optional[str] = None
    template_type: Optional[str] = None
