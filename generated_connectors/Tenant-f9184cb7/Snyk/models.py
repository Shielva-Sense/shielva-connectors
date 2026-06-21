"""Pydantic request/response schemas for the Snyk REST APIs.

Snyk wire format uses snake_case in JSON:API; the connector boundary uses
Dict[str, Any] payloads. These models are connector-local helpers — they are
not the boundary type.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _SnykModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_SnykModel):
    limit: int = 100
    starting_after: Optional[str] = None


class JSONAPIResource(_SnykModel):
    """A single resource object in a JSON:API document."""

    id: str
    type: Optional[str] = None
    attributes: Dict[str, Any] = Field(default_factory=dict)
    relationships: Dict[str, Any] = Field(default_factory=dict)


class JSONAPIDocument(_SnykModel):
    """Top-level JSON:API document with ``data`` + ``links`` + ``meta``."""

    data: Any = None
    links: Dict[str, Any] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)


class OrgResponse(_SnykModel):
    org_id: str = Field(alias="id")
    name: Optional[str] = None
    slug: Optional[str] = None


class ProjectResponse(_SnykModel):
    project_id: str = Field(alias="id")
    name: Optional[str] = None
    type: Optional[str] = None
    origin: Optional[str] = None
    status: Optional[str] = None
    created: Optional[datetime] = None


class IssueResponse(_SnykModel):
    issue_id: str = Field(alias="id")
    title: Optional[str] = None
    severity: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None


class TargetResponse(_SnykModel):
    target_id: str = Field(alias="id")
    display_name: Optional[str] = Field(default=None, alias="display_name")
    source: Optional[str] = None


class PageResult(_SnykModel):
    """Generic paged result with a ``starting_after`` cursor."""

    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_starting_after: Optional[str] = None
