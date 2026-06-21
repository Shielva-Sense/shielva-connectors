"""Pydantic request/response schemas for the Loggly REST API.

Boundary contract: the connector accepts/returns `Dict[str, Any]` payloads; these
schemas exist for caller-side validation and IDE help.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _LogglyModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class SearchParams(_LogglyModel):
    q: str = "*"
    from_: str = Field(default="-24h", alias="from")
    until: str = "now"
    order: str = "desc"
    size: int = 100


class SavedSearchPayload(_LogglyModel):
    name: str
    query: str
    description: Optional[str] = None


class AlertPayload(_LogglyModel):
    name: str
    query: str
    alert_type: str = Field(alias="type")
    threshold_value: int = Field(alias="thresholdValue")
    time_range_minutes: int = Field(alias="timeRange")
    notification_endpoint_ids: List[int] = Field(default_factory=list, alias="endpoints")


class BulkEvent(_LogglyModel):
    """Single line in a bulk-send payload — arbitrary JSON object."""
    timestamp: Optional[datetime] = None
    message: Optional[str] = None


class SearchResponse(_LogglyModel):
    rsid: Dict[str, Any] = Field(default_factory=dict)
    events: List[Dict[str, Any]] = Field(default_factory=list)


class PageResult(_LogglyModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_cursor: Optional[str] = None
