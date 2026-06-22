"""Pydantic request/response schemas for the Honeycomb REST API.

The wire format is snake_case (Honeycomb's choice — unusual for a REST API,
but consistent across every endpoint). Boundary methods accept/return raw
`Dict[str, Any]` payloads; these schemas exist for type-checked construction
of request bodies and for tests.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _HoneycombModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class AuthInfo(_HoneycombModel):
    """GET /auth response shape."""
    api_key_access: Optional[Dict[str, Any]] = None
    team: Optional[Dict[str, Any]] = None
    environment: Optional[Dict[str, Any]] = None


class Dataset(_HoneycombModel):
    """GET /datasets[/{slug}] item."""
    name: str = ""
    slug: str = ""
    description: str = ""
    expand_json_depth: int = 0
    created_at: Optional[datetime] = None
    last_written_at: Optional[datetime] = None
    regular_columns_count: int = 0


class Column(_HoneycombModel):
    """GET /datasets/{slug}/columns item."""
    key_name: str = ""
    type: str = "string"
    description: str = ""
    hidden: bool = False
    last_written: Optional[datetime] = None


class Calculation(_HoneycombModel):
    op: str
    column: Optional[str] = None


class Filter(_HoneycombModel):
    column: str
    op: str
    value: Any = None


class QuerySpec(_HoneycombModel):
    """POST /queries/{slug} request body."""
    breakdowns: List[str] = Field(default_factory=list)
    calculations: List[Dict[str, Any]] = Field(default_factory=list)
    filters: List[Dict[str, Any]] = Field(default_factory=list)
    orders: List[Dict[str, Any]] = Field(default_factory=list)
    having: List[Dict[str, Any]] = Field(default_factory=list)
    time_range: int = 7200
    granularity: int = 0


class QueryResultRequest(_HoneycombModel):
    """POST /query_results/{slug} request body."""
    query_id: str
    disable_series: bool = False
    limit: int = 1000


class Trigger(_HoneycombModel):
    """GET/POST /triggers/{slug} item."""
    name: str = ""
    query_id: str = ""
    threshold: Dict[str, Any] = Field(default_factory=dict)
    frequency: int = 900
    alert_type: str = "on_change"
    recipients: List[Dict[str, Any]] = Field(default_factory=list)


class Marker(_HoneycombModel):
    """GET/POST /markers/{slug} item."""
    message: str = ""
    type: str = "deploy"
    url: Optional[str] = None
    start_time: Optional[int] = None
    end_time: Optional[int] = None


class Board(_HoneycombModel):
    """GET/POST /boards item."""
    name: str = ""
    description: str = ""
    style: str = "list"
    queries: List[Dict[str, Any]] = Field(default_factory=list)


class SLO(_HoneycombModel):
    """GET /slos/{slug} item."""
    name: str = ""
    description: str = ""
    sli: Optional[Dict[str, Any]] = None
    time_period_days: int = 30
    target_per_million: int = 999000


class Recipient(_HoneycombModel):
    """GET /recipients item."""
    type: str = "email"
    target: str = ""


class EventPayload(_HoneycombModel):
    """POST /events/{dataset_slug} body — Honeycomb ingest API.

    Free-form; Honeycomb stores any JSON object as a single event row.
    """
    data: Dict[str, Any] = Field(default_factory=dict)
