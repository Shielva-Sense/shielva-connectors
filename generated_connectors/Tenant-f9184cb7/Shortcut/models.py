"""Pydantic request/response schemas for the Shortcut REST API.

Shortcut uses snake_case on the wire, so no aliasing is needed; the connector
boundary uses ``Dict[str, Any]`` for raw payloads — these models exist
primarily for documentation, validators, and tooling that introspects the
connector module.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _ShortcutModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_ShortcutModel):
    page_size: int = 25
    next: Optional[str] = None


class SearchStoriesRequest(_ShortcutModel):
    query: Optional[str] = None
    page_size: int = 25
    next: Optional[str] = None


class StoryCreateRequest(_ShortcutModel):
    name: str
    story_type: str = "feature"
    project_id: Optional[int] = None
    workflow_state_id: Optional[int] = None
    epic_id: Optional[int] = None
    iteration_id: Optional[int] = None
    description: Optional[str] = None
    estimate: Optional[int] = None
    owner_ids: List[str] = Field(default_factory=list)
    follower_ids: List[str] = Field(default_factory=list)
    labels: List[Dict[str, Any]] = Field(default_factory=list)


class StoryResponse(_ShortcutModel):
    id: int
    name: str
    description: Optional[str] = None
    story_type: Optional[str] = None
    workflow_state_id: Optional[int] = None
    project_id: Optional[int] = None
    epic_id: Optional[int] = None
    iteration_id: Optional[int] = None
    archived: bool = False
    owner_ids: List[str] = Field(default_factory=list)
    requested_by_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    app_url: Optional[str] = None


class EpicCreateRequest(_ShortcutModel):
    name: str
    description: Optional[str] = None
    state: str = "to do"


class EpicResponse(_ShortcutModel):
    id: int
    name: str
    description: Optional[str] = None
    state: Optional[str] = None
    archived: bool = False
    owner_ids: List[str] = Field(default_factory=list)
    app_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class MemberResponse(_ShortcutModel):
    id: str
    name: Optional[str] = None
    mention_name: Optional[str] = None
    email_address: Optional[str] = None
    disabled: bool = False


class WorkflowStateResponse(_ShortcutModel):
    id: int
    name: str
    type: Optional[str] = None
    position: int = 0


class WorkflowResponse(_ShortcutModel):
    id: int
    name: str
    states: List[WorkflowStateResponse] = Field(default_factory=list)


class LabelCreateRequest(_ShortcutModel):
    name: str
    color: Optional[str] = None


class SearchStoriesResponse(_ShortcutModel):
    data: List[Dict[str, Any]] = Field(default_factory=list)
    next: Optional[str] = None
    total: Optional[int] = None
