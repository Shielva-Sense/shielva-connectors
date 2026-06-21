"""Pydantic request/response schemas for the n8n REST API.

The wire format uses camelCase (``workflowId``, ``excludePinnedData``,
``createdAt``). These schemas accept both casings via ``populate_by_name`` so
callers may pass snake_case to the connector while the JSON serialises with the
expected aliases.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _N8nModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_N8nModel):
    limit: int = 100
    cursor: Optional[str] = None


class WorkflowListParams(_N8nModel):
    """Query-string shape for ``GET /workflows``."""

    active: Optional[bool] = None
    tags: Optional[str] = None
    name: Optional[str] = None
    project_id: Optional[str] = Field(default=None, alias="projectId")
    exclude_pinned_data: Optional[bool] = Field(default=None, alias="excludePinnedData")
    limit: int = 100
    cursor: Optional[str] = None


class ExecutionListParams(_N8nModel):
    """Query-string shape for ``GET /executions``."""

    workflow_id: Optional[str] = Field(default=None, alias="workflowId")
    status: Optional[str] = None
    include_data: Optional[bool] = Field(default=None, alias="includeData")
    limit: int = 100
    cursor: Optional[str] = None


class WorkflowResponse(_N8nModel):
    id: str
    name: Optional[str] = None
    active: Optional[bool] = None
    nodes: List[Dict[str, Any]] = Field(default_factory=list)
    connections: Dict[str, Any] = Field(default_factory=dict)
    settings: Optional[Dict[str, Any]] = None
    tags: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: Optional[datetime] = Field(default=None, alias="createdAt")
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")


class ExecutionResponse(_N8nModel):
    id: str
    workflow_id: Optional[str] = Field(default=None, alias="workflowId")
    status: Optional[str] = None
    finished: Optional[bool] = None
    started_at: Optional[datetime] = Field(default=None, alias="startedAt")
    stopped_at: Optional[datetime] = Field(default=None, alias="stoppedAt")


class PageResult(_N8nModel):
    data: List[Dict[str, Any]] = Field(default_factory=list)
    next_cursor: Optional[str] = Field(default=None, alias="nextCursor")
