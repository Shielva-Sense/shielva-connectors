"""Pydantic request/response schemas for the Bitbucket Cloud REST API.

Bitbucket uses snake_case in JSON (`full_name`, `source_branch`,
`merge_strategy`); the connector boundary uses `Dict[str, Any]` payloads
and these models exist only as optional typed views for callers that want
them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _BitbucketModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_BitbucketModel):
    """Bitbucket Cloud paginated-collection paging knobs."""
    pagelen: int = 50
    page: int = 1
    next: Optional[str] = None


class WorkspaceRef(_BitbucketModel):
    slug: str
    uuid: Optional[str] = None
    name: Optional[str] = None


class RepositoryRef(_BitbucketModel):
    full_name: str
    name: Optional[str] = None
    uuid: Optional[str] = None
    is_private: bool = True
    language: Optional[str] = None
    workspace: Optional[WorkspaceRef] = None
    created_on: Optional[datetime] = None
    updated_on: Optional[datetime] = None


class BranchRef(_BitbucketModel):
    name: str
    target: Optional[Dict[str, Any]] = None


class PullRequestEndpoint(_BitbucketModel):
    """`source` / `destination` envelope used by the create-PR body."""
    branch: Dict[str, str] = Field(default_factory=dict)


class CreatePullRequestBody(_BitbucketModel):
    title: str
    source: PullRequestEndpoint
    destination: PullRequestEndpoint
    description: Optional[str] = ""
    reviewers: List[Dict[str, str]] = Field(default_factory=list)
    close_source_branch: bool = False


class MergePullRequestBody(_BitbucketModel):
    merge_strategy: str = "merge_commit"  # merge_commit | squash | fast_forward
    message: Optional[str] = None
    close_source_branch: bool = False


class CreateIssueBody(_BitbucketModel):
    title: str
    priority: str = "minor"
    kind: str = "bug"
    content: Optional[Dict[str, str]] = None  # {"raw": "..."}


class CreateWebhookBody(_BitbucketModel):
    description: str
    url: str
    active: bool = True
    events: List[str] = Field(default_factory=lambda: ["repo:push"])


class PageResult(_BitbucketModel):
    """Generic Bitbucket paginated response envelope."""
    values: List[Dict[str, Any]] = Field(default_factory=list)
    page: int = 1
    pagelen: int = 50
    size: Optional[int] = None
    next: Optional[str] = None
    previous: Optional[str] = None
