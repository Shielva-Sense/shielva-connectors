"""Pydantic request/response schemas for Vanta REST APIs.

camelCase aliases match Vanta wire format; the connector boundary uses
Dict[str, Any] payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _VantaModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class CursorPaging(_VantaModel):
    page_size: int = Field(default=50, alias="pageSize")
    page_cursor: Optional[str] = Field(default=None, alias="pageCursor")


class FrameworkResponse(_VantaModel):
    id: str
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    progress: Optional[float] = None
    certification_status: Optional[str] = Field(default=None, alias="certificationStatus")
    created_at: Optional[datetime] = Field(default=None, alias="createdAt")
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")


class ControlResponse(_VantaModel):
    id: str
    name: str
    description: Optional[str] = None
    framework_id: Optional[str] = Field(default=None, alias="frameworkId")
    control_owner_id: Optional[str] = Field(default=None, alias="controlOwnerId")
    status: Optional[str] = None
    last_tested_at: Optional[datetime] = Field(default=None, alias="lastTestedAt")
    severity: Optional[str] = None


class VendorResponse(_VantaModel):
    id: str
    name: str
    description: Optional[str] = None
    website_url: Optional[str] = Field(default=None, alias="websiteUrl")
    owner_email: Optional[str] = Field(default=None, alias="ownerEmail")
    risk_level: Optional[str] = Field(default=None, alias="riskLevel")
    status: Optional[str] = None


class PersonnelResponse(_VantaModel):
    id: str
    display_name: Optional[str] = Field(default=None, alias="displayName")
    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = Field(default=None, alias="isActive")
    employment_status: Optional[str] = Field(default=None, alias="employmentStatus")


class RiskResponse(_VantaModel):
    id: str
    name: str
    likelihood: Optional[str] = None
    impact: Optional[str] = None
    owner_email: Optional[str] = Field(default=None, alias="ownerEmail")
    status: Optional[str] = None


class IncidentResponse(_VantaModel):
    id: str
    title: Optional[str] = None
    severity: Optional[str] = None
    status: Optional[str] = None
    detected_at: Optional[datetime] = Field(default=None, alias="detectedAt")
    resolved_at: Optional[datetime] = Field(default=None, alias="resolvedAt")


class DocumentResponse(_VantaModel):
    id: str
    title: Optional[str] = None
    version: Optional[str] = None
    url: Optional[str] = None
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")


class FindingResponse(_VantaModel):
    id: str
    title: Optional[str] = None
    severity: Optional[str] = None
    status: Optional[str] = None
    control_id: Optional[str] = Field(default=None, alias="controlId")
    assignee_email: Optional[str] = Field(default=None, alias="assigneeEmail")


class PageResult(_VantaModel):
    results: List[Dict[str, Any]] = Field(default_factory=list)
    page_info: Dict[str, Any] = Field(default_factory=dict, alias="pageInfo")
    next_page_cursor: Optional[str] = Field(default=None, alias="nextPageCursor")
