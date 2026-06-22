"""Pydantic request/response schemas for the Drip v2 REST API.

Drip uses snake_case on the wire; these schemas describe the most-handled
envelopes for documentation and type-hint purposes. The connector boundary
accepts/returns ``Dict[str, Any]`` payloads — these models are reference
shapes, not strict validation gates.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _DripModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_DripModel):
    page: int = 1
    per_page: int = 50


class SubscriberRequest(_DripModel):
    """Body for POST /subscribers — wrapped in ``{subscribers:[…]}`` envelope."""

    email: str
    custom_fields: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    time_zone: Optional[str] = None
    ip_address: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None


class SubscriberResponse(_DripModel):
    subscriber_id: str = Field(alias="id")
    email: Optional[str] = None
    status: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    custom_fields: Dict[str, Any] = Field(default_factory=dict)
    time_zone: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class EventRequest(_DripModel):
    """Body for POST /events — wrapped in ``{events:[…]}`` envelope."""

    email: str
    action: str
    properties: Dict[str, Any] = Field(default_factory=dict)
    occurred_at: Optional[str] = None


class TagRequest(_DripModel):
    """Body for POST /tags — ``{tags:[{email,tag}]}`` envelope."""

    email: str
    tag: str


class CampaignResponse(_DripModel):
    campaign_id: int = Field(alias="id")
    name: Optional[str] = None
    status: Optional[str] = None
    from_name: Optional[str] = None
    from_email: Optional[str] = None
    subject: Optional[str] = None
    created_at: Optional[datetime] = None


class OrderRequest(_DripModel):
    """Body for POST /orders — wrapped in ``{orders:[…]}`` envelope."""

    email: str
    provider: Optional[str] = None
    provider_order_id: Optional[str] = None
    amount: Optional[int] = None
    currency: Optional[str] = None
    occurred_at: Optional[str] = None
    items: List[Dict[str, Any]] = Field(default_factory=list)


class OrderResponse(_DripModel):
    order_id: str = Field(alias="id")
    email: Optional[str] = None
    provider: Optional[str] = None
    provider_order_id: Optional[str] = None
    amount: Optional[int] = None
    currency: Optional[str] = None
    financial_state: Optional[str] = None
    fulfillment_state: Optional[str] = None
    occurred_at: Optional[datetime] = None
    items: List[Dict[str, Any]] = Field(default_factory=list)


class WorkflowResponse(_DripModel):
    workflow_id: int = Field(alias="id")
    name: Optional[str] = None
    state: Optional[str] = None
    created_at: Optional[datetime] = None


class PageResult(_DripModel):
    """Generic page envelope (``meta.page`` / ``meta.total_pages``)."""

    items: List[Dict[str, Any]] = Field(default_factory=list)
    page: int = 1
    total_pages: int = 1
    total_count: Optional[int] = None
