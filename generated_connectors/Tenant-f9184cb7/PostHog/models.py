"""Pydantic request/response schemas for the PostHog REST + Capture API.

snake_case wire format; the connector boundary uses Dict[str, Any] payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _PostHogModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class CaptureRequest(_PostHogModel):
    """Single event payload for /capture/."""

    api_key: str
    event: str
    distinct_id: str
    properties: Dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[str] = None


class BatchRequest(_PostHogModel):
    """Batched payload for /batch/."""

    api_key: str
    batch: List[Dict[str, Any]] = Field(default_factory=list)


class FeatureFlagCreate(_PostHogModel):
    key: str
    name: str
    active: bool = True
    filters: Dict[str, Any] = Field(
        default_factory=lambda: {"groups": [{"properties": [], "rollout_percentage": 100}]}
    )


class FeatureFlagResponse(_PostHogModel):
    id: int
    key: str
    name: Optional[str] = None
    active: bool = True
    filters: Dict[str, Any] = Field(default_factory=dict)
    deleted: bool = False
    created_at: Optional[datetime] = None


class PersonResponse(_PostHogModel):
    id: str
    distinct_ids: List[str] = Field(default_factory=list)
    properties: Dict[str, Any] = Field(default_factory=dict)
    is_identified: bool = False
    created_at: Optional[datetime] = None


class EventResponse(_PostHogModel):
    id: str
    event: str
    distinct_id: str
    properties: Dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[datetime] = None


class ProjectResponse(_PostHogModel):
    id: int
    name: str
    api_token: Optional[str] = None
    organization: Optional[str] = None


class PageResult(_PostHogModel):
    results: List[Dict[str, Any]] = Field(default_factory=list)
    next: Optional[str] = None
    previous: Optional[str] = None
    count: Optional[int] = None
