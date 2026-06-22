"""Pydantic request/response schemas for Hubstaff REST APIs.

Hubstaff uses snake_case in JSON; the connector boundary passes payloads through
as `Dict[str, Any]`. These schemas exist for static-doc generation and optional
validation, not as required serialization gates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _HubstaffModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_HubstaffModel):
    page_limit: int = 50
    page_start_id: Optional[int] = None


class OrganizationResponse(_HubstaffModel):
    organization_id: int = Field(alias="id")
    name: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ProjectResponse(_HubstaffModel):
    project_id: int = Field(alias="id")
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    organization_id: Optional[int] = None
    billable: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TaskResponse(_HubstaffModel):
    task_id: int = Field(alias="id")
    summary: Optional[str] = None
    project_id: Optional[int] = None
    assignee_id: Optional[int] = None
    status: Optional[str] = None
    due_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DailyActivityResponse(_HubstaffModel):
    activity_id: int = Field(alias="id")
    user_id: Optional[int] = None
    project_id: Optional[int] = None
    task_id: Optional[int] = None
    tracked: int = 0
    idle: int = 0
    keyboard: int = 0
    mouse: int = 0
    overall: int = 0
    date: Optional[datetime] = None
    starts_at: Optional[datetime] = None


class PageResult(_HubstaffModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_page_start_id: Optional[int] = None
