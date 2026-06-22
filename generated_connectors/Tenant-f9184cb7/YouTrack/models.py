"""Pydantic request/response schemas for YouTrack REST APIs.

camelCase aliases match YouTrack wire format; the connector boundary uses
`Dict[str, Any]` payloads.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _YouTrackModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_YouTrackModel):
    skip: int = Field(default=0, alias="$skip")
    top: int = Field(default=100, alias="$top")
    fields: Optional[str] = None


class UserResponse(_YouTrackModel):
    user_id: str = Field(alias="id")
    login: Optional[str] = None
    full_name: Optional[str] = Field(default=None, alias="fullName")
    email: Optional[str] = None
    banned: Optional[bool] = None


class ProjectResponse(_YouTrackModel):
    project_id: str = Field(alias="id")
    short_name: Optional[str] = Field(default=None, alias="shortName")
    name: Optional[str] = None
    description: Optional[str] = None
    archived: Optional[bool] = None


class IssueResponse(_YouTrackModel):
    issue_id: str = Field(alias="id")
    id_readable: Optional[str] = Field(default=None, alias="idReadable")
    summary: Optional[str] = None
    description: Optional[str] = None
    created: Optional[int] = None
    updated: Optional[int] = None
    reporter: Optional[Dict[str, Any]] = None
    custom_fields: List[Dict[str, Any]] = Field(default_factory=list, alias="customFields")


class CreateIssueBody(_YouTrackModel):
    project: Dict[str, str]
    summary: str
    description: str = ""
    custom_fields: List[Dict[str, Any]] = Field(default_factory=list, alias="customFields")


class CommentBody(_YouTrackModel):
    text: str
