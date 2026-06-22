"""Pydantic request/response schemas for GoHighLevel REST APIs.

camelCase aliases match HighLevel wire format; the connector boundary uses
Dict[str, Any] payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _GHLModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_GHLModel):
    limit: int = 20
    page: int = 1


class ContactResponse(_GHLModel):
    contact_id: str = Field(alias="id")
    location_id: Optional[str] = Field(default=None, alias="locationId")
    contact_name: Optional[str] = Field(default=None, alias="contactName")
    first_name: Optional[str] = Field(default=None, alias="firstName")
    last_name: Optional[str] = Field(default=None, alias="lastName")
    email: Optional[str] = None
    phone: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    date_added: Optional[datetime] = Field(default=None, alias="dateAdded")
    date_updated: Optional[datetime] = Field(default=None, alias="dateUpdated")


class OpportunityResponse(_GHLModel):
    opportunity_id: str = Field(alias="id")
    name: Optional[str] = None
    status: Optional[str] = None
    monetary_value: Optional[float] = Field(default=None, alias="monetaryValue")
    pipeline_id: Optional[str] = Field(default=None, alias="pipelineId")
    pipeline_stage_id: Optional[str] = Field(default=None, alias="pipelineStageId")
    contact_id: Optional[str] = Field(default=None, alias="contactId")
    source: Optional[str] = None
    created_at: Optional[datetime] = Field(default=None, alias="createdAt")
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")


class ConversationResponse(_GHLModel):
    conversation_id: str = Field(alias="id")
    contact_id: Optional[str] = Field(default=None, alias="contactId")
    location_id: Optional[str] = Field(default=None, alias="locationId")
    type: Optional[str] = None
    last_message_body: Optional[str] = Field(default=None, alias="lastMessageBody")
    last_message_type: Optional[str] = Field(default=None, alias="lastMessageType")
    last_message_date: Optional[datetime] = Field(default=None, alias="lastMessageDate")
    unread_count: int = Field(default=0, alias="unreadCount")


class PageResult(_GHLModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_page: Optional[int] = None
