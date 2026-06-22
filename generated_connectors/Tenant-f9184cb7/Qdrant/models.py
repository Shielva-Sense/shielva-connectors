"""Pydantic request/response schemas for Qdrant REST APIs.

snake_case wire format mirrored verbatim. Connector boundary uses
Dict[str, Any] payloads — these models are convenience containers for
typed call-sites, not the canonical contract.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _QdrantModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class VectorParams(_QdrantModel):
    """Single-vector spec: `{size: 768, distance: "Cosine"}`."""

    size: int
    distance: str = "Cosine"
    hnsw_config: Optional[Dict[str, Any]] = None
    quantization_config: Optional[Dict[str, Any]] = None
    on_disk: Optional[bool] = None


class CollectionConfig(_QdrantModel):
    """Collection config envelope returned by `GET /collections/{name}`."""

    params: Dict[str, Any] = Field(default_factory=dict)
    hnsw_config: Optional[Dict[str, Any]] = None
    optimizer_config: Optional[Dict[str, Any]] = None
    wal_config: Optional[Dict[str, Any]] = None


class CollectionResponse(_QdrantModel):
    """`GET /collections/{name}` result body."""

    status: Optional[str] = None
    optimizer_status: Optional[Any] = None
    vectors_count: Optional[int] = None
    indexed_vectors_count: Optional[int] = None
    points_count: Optional[int] = None
    segments_count: Optional[int] = None
    config: Optional[CollectionConfig] = None
    payload_schema: Dict[str, Any] = Field(default_factory=dict)


class PointStruct(_QdrantModel):
    """Single upsert/retrieve point shape."""

    id: Any  # int or UUID string
    vector: List[float] = Field(default_factory=list)
    payload: Optional[Dict[str, Any]] = None


class ScoredPoint(_QdrantModel):
    """Search/recommend result item."""

    id: Any
    score: float
    payload: Optional[Dict[str, Any]] = None
    vector: Optional[List[float]] = None
    version: Optional[int] = None


class ScrollResult(_QdrantModel):
    """`POST /points/scroll` result body."""

    points: List[Dict[str, Any]] = Field(default_factory=list)
    next_page_offset: Optional[Any] = None
