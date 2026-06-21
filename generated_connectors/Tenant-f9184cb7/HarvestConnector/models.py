"""Pydantic request/response schemas for the Harvest REST API.

Harvest uses snake_case JSON; aliases are not strictly required, but we
declare schemas so call sites can validate before sending.
The connector boundary continues to accept/return `Dict[str, Any]`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _HarvestModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_HarvestModel):
    page: int = 1
    per_page: int = 100


class PagedResponse(_HarvestModel):
    """Generic Harvest paginated envelope."""

    per_page: int = 100
    total_pages: int = 1
    total_entries: int = 0
    next_page: Optional[int] = None
    previous_page: Optional[int] = None
    page: int = 1


class TimeEntryCreate(_HarvestModel):
    project_id: int
    task_id: int
    spent_date: str  # YYYY-MM-DD
    hours: Optional[float] = None
    notes: Optional[str] = None
    user_id: Optional[int] = None


class TimeEntryResponse(_HarvestModel):
    id: int
    spent_date: Optional[str] = None
    hours: float = 0.0
    notes: Optional[str] = None
    is_locked: bool = False
    is_billed: bool = False
    project: Optional[Dict[str, Any]] = None
    task: Optional[Dict[str, Any]] = None
    user: Optional[Dict[str, Any]] = None
    client: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class InvoiceResponse(_HarvestModel):
    id: int
    number: Optional[str] = None
    state: Optional[str] = None  # draft / open / paid / closed
    amount: float = 0.0
    currency: str = "USD"
    issue_date: Optional[str] = None
    due_date: Optional[str] = None
    paid_date: Optional[str] = None
    client: Optional[Dict[str, Any]] = None
    line_items: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ClientResponse(_HarvestModel):
    id: int
    name: Optional[str] = None
    is_active: bool = True
    currency: str = "USD"
    address: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ProjectResponse(_HarvestModel):
    id: int
    name: Optional[str] = None
    code: Optional[str] = None
    is_active: bool = True
    is_billable: bool = True
    billing_method: Optional[str] = None
    budget_by: Optional[str] = None
    client: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
