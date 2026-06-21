"""Pydantic request/response schemas for Workato REST APIs.

snake_case fields match Workato wire format; the connector boundary uses
Dict[str, Any] payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _WorkatoModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_WorkatoModel):
    page: int = 1
    per_page: int = 100


class RecipeResponse(_WorkatoModel):
    id: int
    name: str
    user_id: Optional[int] = None
    folder_id: Optional[int] = None
    running: Optional[bool] = None
    job_succeeded_count: Optional[int] = None
    job_failed_count: Optional[int] = None
    description: Optional[str] = None
    version_no: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ConnectionResponse(_WorkatoModel):
    id: int
    name: str
    provider: Optional[str] = None
    application: Optional[str] = None
    authorization_status: Optional[str] = None
    folder_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class JobResponse(_WorkatoModel):
    id: int
    flow_run_id: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None
    recipe_id: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class FolderResponse(_WorkatoModel):
    id: int
    name: str
    parent_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PageResult(_WorkatoModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    page: Optional[int] = None
    per_page: Optional[int] = None
    total: Optional[int] = None
