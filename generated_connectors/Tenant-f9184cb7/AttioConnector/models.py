"""Pydantic request/response schemas for Attio REST API.

Attio uses snake_case in the wire format (``record_id``, ``parent_object``,
``last_modified_at``). The connector boundary uses ``Dict[str, Any]`` payloads;
these models exist so callers that want typed access can opt-in.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _AttioModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_AttioModel):
    limit: int = 50
    offset: int = 0


class QueryRequest(_AttioModel):
    """Generic Attio Query API body."""

    filter: Dict[str, Any] = Field(default_factory=dict)
    sorts: List[Dict[str, Any]] = Field(default_factory=list)
    limit: int = 50
    offset: int = 0


class WorkspaceResponse(_AttioModel):
    workspace_id: str = Field(alias="workspace_id")
    name: Optional[str] = None
    slug: Optional[str] = None


class RecordResponse(_AttioModel):
    id: Dict[str, Any] = Field(default_factory=dict)
    values: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class NoteResponse(_AttioModel):
    id: Dict[str, Any] = Field(default_factory=dict)
    title: Optional[str] = None
    content_plaintext: Optional[str] = None
    content_markdown: Optional[str] = None
    parent_object: Optional[str] = None
    parent_record_id: Optional[str] = None
    created_at: Optional[datetime] = None


class TaskResponse(_AttioModel):
    id: Dict[str, Any] = Field(default_factory=dict)
    content_plaintext: Optional[str] = None
    is_completed: Optional[bool] = None
    deadline_at: Optional[datetime] = None
    linked_records: List[Dict[str, Any]] = Field(default_factory=list)
    assignees: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: Optional[datetime] = None


class PageResult(_AttioModel):
    data: List[Dict[str, Any]] = Field(default_factory=list)
    next_offset: Optional[int] = None
