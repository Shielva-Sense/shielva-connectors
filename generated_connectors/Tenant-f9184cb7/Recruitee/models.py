"""Pydantic request/response schemas for Recruitee REST APIs.

snake_case fields match the Recruitee wire format. The connector boundary
uses `Dict[str, Any]` payloads; these models exist for callers that prefer
typed DTOs and for documentation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _RecruiteeModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_RecruiteeModel):
    limit: int = 50
    offset: int = 0


class CandidateResponse(_RecruiteeModel):
    id: int
    name: str = ""
    emails: List[Dict[str, Any]] = Field(default_factory=list)
    phones: List[Dict[str, Any]] = Field(default_factory=list)
    source: Optional[str] = None
    photo_thumb_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class OfferResponse(_RecruiteeModel):
    id: int
    title: str = ""
    status: str = ""
    position_type: Optional[str] = None
    employment_type_code: Optional[str] = None
    department_id: Optional[int] = None
    location_ids: List[int] = Field(default_factory=list)
    description: Optional[str] = None
    requirements: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class NoteResponse(_RecruiteeModel):
    id: int
    body: str = ""
    candidate_id: Optional[int] = None
    visible_to_team_id: Optional[int] = None
    created_at: Optional[datetime] = None


class TagResponse(_RecruiteeModel):
    id: int
    name: str = ""


class DepartmentResponse(_RecruiteeModel):
    id: int
    name: str = ""


class PageResult(_RecruiteeModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    total: Optional[int] = None
