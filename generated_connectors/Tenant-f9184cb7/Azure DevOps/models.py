"""Pydantic request/response schemas for the Azure DevOps REST APIs.

camelCase aliases match Azure DevOps wire format; the connector boundary
exposes snake_case and serialises via `by_alias=True`. All models tolerate
unknown fields (`extra="allow"`) — Azure DevOps payloads carry many optional
metadata fields we don't want to enumerate exhaustively.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _AzureDevopsModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


# ── Projects + Teams + Users ────────────────────────────────────────────────


class ProjectResponse(_AzureDevopsModel):
    id: str
    name: str
    description: Optional[str] = None
    url: Optional[str] = None
    state: Optional[str] = None
    revision: Optional[int] = None
    visibility: Optional[str] = None
    last_update_time: Optional[datetime] = Field(default=None, alias="lastUpdateTime")


class TeamResponse(_AzureDevopsModel):
    id: str
    name: str
    description: Optional[str] = None
    url: Optional[str] = None
    identity_url: Optional[str] = Field(default=None, alias="identityUrl")
    project_id: Optional[str] = Field(default=None, alias="projectId")
    project_name: Optional[str] = Field(default=None, alias="projectName")


class GraphUserResponse(_AzureDevopsModel):
    descriptor: Optional[str] = None
    subject_kind: Optional[str] = Field(default=None, alias="subjectKind")
    display_name: Optional[str] = Field(default=None, alias="displayName")
    mail_address: Optional[str] = Field(default=None, alias="mailAddress")
    principal_name: Optional[str] = Field(default=None, alias="principalName")
    origin: Optional[str] = None
    origin_id: Optional[str] = Field(default=None, alias="originId")


# ── Repos + Pull Requests ───────────────────────────────────────────────────


class RepositoryResponse(_AzureDevopsModel):
    id: str
    name: str
    url: Optional[str] = None
    project: Optional[Dict[str, Any]] = None
    default_branch: Optional[str] = Field(default=None, alias="defaultBranch")
    size: Optional[int] = None
    remote_url: Optional[str] = Field(default=None, alias="remoteUrl")
    ssh_url: Optional[str] = Field(default=None, alias="sshUrl")
    web_url: Optional[str] = Field(default=None, alias="webUrl")
    is_disabled: Optional[bool] = Field(default=None, alias="isDisabled")


class CreatePullRequestRequest(_AzureDevopsModel):
    title: str
    source_ref_name: str = Field(alias="sourceRefName")
    target_ref_name: str = Field(alias="targetRefName")
    description: Optional[str] = ""
    reviewers: List[Dict[str, Any]] = Field(default_factory=list)
    is_draft: Optional[bool] = Field(default=None, alias="isDraft")


class PullRequestResponse(_AzureDevopsModel):
    pull_request_id: int = Field(alias="pullRequestId")
    code_review_id: Optional[int] = Field(default=None, alias="codeReviewId")
    status: Optional[str] = None
    created_by: Optional[Dict[str, Any]] = Field(default=None, alias="createdBy")
    creation_date: Optional[datetime] = Field(default=None, alias="creationDate")
    title: Optional[str] = None
    description: Optional[str] = None
    source_ref_name: Optional[str] = Field(default=None, alias="sourceRefName")
    target_ref_name: Optional[str] = Field(default=None, alias="targetRefName")
    merge_status: Optional[str] = Field(default=None, alias="mergeStatus")
    is_draft: Optional[bool] = Field(default=None, alias="isDraft")
    url: Optional[str] = None


# ── Work Items + WIQL ───────────────────────────────────────────────────────


class WiqlQueryRequest(_AzureDevopsModel):
    query: str


class WorkItemReference(_AzureDevopsModel):
    id: int
    url: Optional[str] = None


class WiqlQueryResponse(_AzureDevopsModel):
    query_type: Optional[str] = Field(default=None, alias="queryType")
    query_result_type: Optional[str] = Field(default=None, alias="queryResultType")
    as_of: Optional[datetime] = Field(default=None, alias="asOf")
    columns: List[Dict[str, Any]] = Field(default_factory=list)
    work_items: List[WorkItemReference] = Field(default_factory=list, alias="workItems")


class WorkItemPatch(_AzureDevopsModel):
    """One JSON-patch operation against /fields/<name>."""

    op: str
    path: str
    value: Any = None
    from_: Optional[str] = Field(default=None, alias="from")


class WorkItemResponse(_AzureDevopsModel):
    id: int
    rev: Optional[int] = None
    fields: Dict[str, Any] = Field(default_factory=dict)
    url: Optional[str] = None
    links: Optional[Dict[str, Any]] = Field(default=None, alias="_links")


# ── Builds + Pipelines ──────────────────────────────────────────────────────


class QueueBuildRequest(_AzureDevopsModel):
    definition: Dict[str, Any]
    source_branch: Optional[str] = Field(default=None, alias="sourceBranch")
    parameters: Optional[str] = None


class BuildResponse(_AzureDevopsModel):
    id: int
    build_number: Optional[str] = Field(default=None, alias="buildNumber")
    status: Optional[str] = None
    result: Optional[str] = None
    queue_time: Optional[datetime] = Field(default=None, alias="queueTime")
    start_time: Optional[datetime] = Field(default=None, alias="startTime")
    finish_time: Optional[datetime] = Field(default=None, alias="finishTime")
    source_branch: Optional[str] = Field(default=None, alias="sourceBranch")
    source_version: Optional[str] = Field(default=None, alias="sourceVersion")
    definition: Optional[Dict[str, Any]] = None
    project: Optional[Dict[str, Any]] = None


class PipelineResponse(_AzureDevopsModel):
    id: int
    name: str
    folder: Optional[str] = None
    revision: Optional[int] = None
    url: Optional[str] = None


# ── Releases ────────────────────────────────────────────────────────────────


class ReleaseResponse(_AzureDevopsModel):
    id: int
    name: Optional[str] = None
    status: Optional[str] = None
    created_on: Optional[datetime] = Field(default=None, alias="createdOn")
    modified_on: Optional[datetime] = Field(default=None, alias="modifiedOn")
    release_definition: Optional[Dict[str, Any]] = Field(default=None, alias="releaseDefinition")


# ── Generic envelopes ───────────────────────────────────────────────────────


class ListEnvelope(_AzureDevopsModel):
    """Generic `{count, value: [...]}` envelope Azure DevOps returns for list APIs."""

    count: Optional[int] = None
    value: List[Dict[str, Any]] = Field(default_factory=list)
    continuation_token: Optional[str] = Field(default=None, alias="continuationToken")
