"""Pydantic request/response schemas for Toggl Track REST APIs.

Toggl uses snake_case in JSON; the connector boundary uses Dict[str, Any]
payloads. These models are kept for typed callers that prefer parsed
objects.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _TogglModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_TogglModel):
    page: int = 1
    per_page: int = 50


class MeResponse(_TogglModel):
    id: int
    email: Optional[str] = None
    fullname: Optional[str] = None
    default_workspace_id: Optional[int] = None
    timezone: Optional[str] = None


class WorkspaceResponse(_TogglModel):
    id: int
    name: Optional[str] = None
    organization_id: Optional[int] = None
    admin: Optional[bool] = None
    role: Optional[str] = None


class ProjectResponse(_TogglModel):
    id: int
    workspace_id: Optional[int] = None
    client_id: Optional[int] = None
    name: Optional[str] = None
    active: Optional[bool] = None
    billable: Optional[bool] = None
    color: Optional[str] = None
    created_at: Optional[datetime] = None
    at: Optional[datetime] = None


class TimeEntryResponse(_TogglModel):
    id: int
    workspace_id: Optional[int] = None
    project_id: Optional[int] = None
    task_id: Optional[int] = None
    user_id: Optional[int] = None
    description: Optional[str] = None
    start: Optional[datetime] = None
    stop: Optional[datetime] = None
    duration: Optional[int] = None
    billable: Optional[bool] = None
    tags: List[str] = Field(default_factory=list)


class TimeEntryCreate(_TogglModel):
    workspace_id: int
    description: Optional[str] = None
    start: Optional[str] = None
    stop: Optional[str] = None
    duration: Optional[int] = None
    project_id: Optional[int] = None
    task_id: Optional[int] = None
    billable: Optional[bool] = None
    tags: List[str] = Field(default_factory=list)
    created_with: str = "shielva-toggl-connector"


class ProjectCreate(_TogglModel):
    name: str
    active: Optional[bool] = True
    is_private: Optional[bool] = True
    client_id: Optional[int] = None
    color: Optional[str] = None


class ClientCreate(_TogglModel):
    name: str
    notes: Optional[str] = None


class PageResult(_TogglModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_page: Optional[int] = None
