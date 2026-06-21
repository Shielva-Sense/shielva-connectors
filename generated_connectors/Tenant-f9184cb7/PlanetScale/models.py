"""Pydantic request/response schemas for PlanetScale REST APIs.

PlanetScale's wire format is snake_case (Python-friendly). The connector
boundary uses ``Dict[str, Any]`` payloads — these models exist for typed
callers (e.g. the agentic builder, ARC action runtime) that want validated
request envelopes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _PlanetScaleModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


# ── Pagination ──────────────────────────────────────────────────────────────


class Paging(_PlanetScaleModel):
    """PlanetScale page-based pagination."""

    page: int = 1
    per_page: int = 25


# ── Request bodies ──────────────────────────────────────────────────────────


class CreateDatabaseRequest(_PlanetScaleModel):
    name: str
    plan: str = "hobby"
    cluster_size: str = "PS_10"
    region: Optional[Dict[str, Any]] = None


class CreateBranchRequest(_PlanetScaleModel):
    name: str
    parent_branch: str = "main"
    backup_id: Optional[str] = None


class CreateDeployRequestBody(_PlanetScaleModel):
    branch: str
    into_branch: str = "main"
    notes: Optional[str] = None


# ── Response envelopes ──────────────────────────────────────────────────────


class OrganizationResponse(_PlanetScaleModel):
    id: Optional[str] = None
    name: str
    display_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DatabaseResponse(_PlanetScaleModel):
    id: Optional[str] = None
    name: str
    plan: Optional[str] = None
    state: Optional[str] = None
    region: Optional[Dict[str, Any]] = None
    html_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BranchResponse(_PlanetScaleModel):
    id: Optional[str] = None
    name: str
    parent_branch: Optional[str] = None
    production: bool = False
    ready: bool = False
    html_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DeployRequestResponse(_PlanetScaleModel):
    id: Optional[str] = None
    number: int
    branch: Optional[str] = None
    into_branch: Optional[str] = Field(default=None, alias="into_branch")
    state: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None


class BackupResponse(_PlanetScaleModel):
    id: Optional[str] = None
    name: Optional[str] = None
    state: Optional[str] = None
    size: Optional[int] = None
    created_at: Optional[datetime] = None


class DatabaseTokenResponse(_PlanetScaleModel):
    """A PlanetScale branch password / database token."""

    id: Optional[str] = None
    name: str
    role: str = "reader"
    plain_text: Optional[str] = None  # populated only at create time
    created_at: Optional[datetime] = None


class PageResult(_PlanetScaleModel):
    data: List[Dict[str, Any]] = Field(default_factory=list)
    has_next: bool = False
    has_prev: bool = False
    next_page: Optional[int] = None
    prev_page: Optional[int] = None
