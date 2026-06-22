"""Pydantic request/response schemas for HuggingFace REST APIs.

The connector boundary uses Dict[str, Any] payloads; these schemas exist as
typed contracts for documentation and optional internal validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _HFModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ListModelsRequest(_HFModel):
    search: Optional[str] = None
    author: Optional[str] = None
    filter: Optional[str] = None
    limit: int = 20
    sort: str = "downloads"


class HubModel(_HFModel):
    """A single Hub model document."""

    id: str
    author: Optional[str] = None
    downloads: Optional[int] = None
    likes: Optional[int] = None
    tags: List[str] = Field(default_factory=list)
    pipeline_tag: Optional[str] = None
    created_at: Optional[datetime] = Field(default=None, alias="createdAt")
    last_modified: Optional[datetime] = Field(default=None, alias="lastModified")


class HubDataset(_HFModel):
    id: str
    downloads: Optional[int] = None
    tags: List[str] = Field(default_factory=list)


class HubSpace(_HFModel):
    id: str
    likes: Optional[int] = None


class InferenceRequest(_HFModel):
    """Generic Inference API JSON body."""

    inputs: Any
    parameters: Optional[Dict[str, Any]] = None
    options: Optional[Dict[str, Any]] = None


class EndpointSpec(_HFModel):
    """Inference Endpoints create payload."""

    name: str
    accountId: Optional[str] = None
    type: Optional[str] = "protected"
    provider: Dict[str, Any] = Field(default_factory=dict)
    compute: Dict[str, Any] = Field(default_factory=dict)
    model: Dict[str, Any] = Field(default_factory=dict)


class InferenceResponse(_HFModel):
    """Generic wrapper around any inference-API response payload."""

    raw: Any
