"""Local dataclass + pydantic schemas for the Supabase connector.

These are request-shape mirrors used by callers that want a strongly-typed
builder pattern. The connector boundary itself accepts `Dict[str, Any]`
payloads, mirroring the Wix / Bandwidth conventions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Pydantic request shapes ────────────────────────────────────────────────


class _SupabaseModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class SelectQuery(_SupabaseModel):
    """Builder for a PostgREST SELECT call."""
    table: str
    columns: str = "*"
    filter: Dict[str, Any] = Field(default_factory=dict)
    order: Optional[str] = None
    limit: Optional[int] = None
    offset: Optional[int] = None


class InsertBody(_SupabaseModel):
    table: str
    rows: List[Dict[str, Any]]
    returning: str = "representation"


class UpdateBody(_SupabaseModel):
    table: str
    filter: Dict[str, Any]
    fields: Dict[str, Any]


class DeleteBody(_SupabaseModel):
    table: str
    filter: Dict[str, Any]


class UpsertBody(_SupabaseModel):
    table: str
    rows: List[Dict[str, Any]]
    on_conflict: Optional[str] = None


class RpcBody(_SupabaseModel):
    function_name: str
    params: Dict[str, Any] = Field(default_factory=dict)


class CreateUserBody(_SupabaseModel):
    email: str
    password: Optional[str] = None
    user_metadata: Optional[Dict[str, Any]] = None
    email_confirm: Optional[bool] = None


class UpdateUserBody(_SupabaseModel):
    user_id: str
    attrs: Dict[str, Any] = Field(default_factory=dict)


class UploadObjectBody(_SupabaseModel):
    bucket: str
    path: str
    # content is supplied separately (bytes); pydantic mirrors metadata only
    content_type: str = "application/octet-stream"
    upsert: bool = False
    cache_control: Optional[str] = None


# ── Lightweight dataclass shims for back-compat with the previous build ────


@dataclass
class SupabaseAuthState:
    """Mirror of the shared AuthStatus enum value."""

    status: str = "pending"
    message: str = ""

    @property
    def AuthStatus(self) -> str:  # noqa: N802 — legacy alias
        return self.status


@dataclass
class SupabaseHealthState:
    """Mirror of the shared ConnectorHealth enum value."""

    health: str = "healthy"
    message: str = ""

    @property
    def ConnectorHealth(self) -> str:  # noqa: N802 — legacy alias
        return self.health


@dataclass
class StorageBucket:
    id: str
    name: str
    public: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
