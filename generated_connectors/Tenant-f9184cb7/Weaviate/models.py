"""Pydantic request/response schemas for Weaviate REST + GraphQL.

camelCase aliases match Weaviate wire format; the connector boundary uses
Dict[str, Any] payloads.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _WeaviateModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ClassDefinition(_WeaviateModel):
    """A Weaviate class (collection) definition."""

    class_name: str = Field(alias="class")
    description: Optional[str] = None
    properties: List[Dict[str, Any]] = Field(default_factory=list)
    vectorizer: Optional[str] = None
    vector_index_type: Optional[str] = Field(default=None, alias="vectorIndexType")
    vector_index_config: Optional[Dict[str, Any]] = Field(default=None, alias="vectorIndexConfig")
    sharding_config: Optional[Dict[str, Any]] = Field(default=None, alias="shardingConfig")
    multi_tenancy_config: Optional[Dict[str, Any]] = Field(default=None, alias="multiTenancyConfig")


class WeaviateObject(_WeaviateModel):
    """A single Weaviate object."""

    object_id: Optional[str] = Field(default=None, alias="id")
    class_name: str = Field(alias="class")
    properties: Dict[str, Any] = Field(default_factory=dict)
    vector: Optional[List[float]] = None
    tenant: Optional[str] = None
    creation_time_unix: Optional[int] = Field(default=None, alias="creationTimeUnix")
    last_update_time_unix: Optional[int] = Field(default=None, alias="lastUpdateTimeUnix")
    additional: Optional[Dict[str, Any]] = None


class BatchObjectsRequest(_WeaviateModel):
    objects: List[Dict[str, Any]] = Field(default_factory=list)


class GraphQLRequest(_WeaviateModel):
    query: str
    variables: Optional[Dict[str, Any]] = None


class TenantDefinition(_WeaviateModel):
    name: str
    activity_status: Optional[str] = Field(default=None, alias="activityStatus")


class BackupRequest(_WeaviateModel):
    backup_id: str = Field(alias="id")
    include: Optional[List[str]] = None
    exclude: Optional[List[str]] = None
