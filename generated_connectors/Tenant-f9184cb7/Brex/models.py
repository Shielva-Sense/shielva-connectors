"""Pydantic request/response schemas for Brex REST APIs.

snake_case field names mirror Brex wire format; the connector boundary
uses Dict[str, Any] payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _BrexModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_BrexModel):
    limit: int = 50
    cursor: Optional[str] = None


class Amount(_BrexModel):
    amount: Optional[int] = None  # cents
    currency: Optional[str] = None


class BrexUser(_BrexModel):
    id: str
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    status: Optional[str] = None
    role: Optional[str] = None
    department_id: Optional[str] = None
    location_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class BrexCard(_BrexModel):
    id: str
    last_four: Optional[str] = None
    card_name: Optional[str] = None
    card_type: Optional[str] = None
    status: Optional[str] = None
    limit_type: Optional[str] = None
    owner: Optional[Dict[str, Any]] = None


class BrexTransaction(_BrexModel):
    id: str
    description: Optional[str] = None
    amount: Optional[Amount] = None
    type: Optional[str] = None
    card_id: Optional[str] = None
    posted_at_date: Optional[datetime] = None
    initiated_at_date: Optional[datetime] = None
    merchant: Optional[Dict[str, Any]] = None


class BrexExpense(_BrexModel):
    id: str
    memo: Optional[str] = None
    category: Optional[str] = None
    amount: Optional[Amount] = None
    status: Optional[str] = None
    payment_status: Optional[str] = None
    expense_type: Optional[str] = None
    user_id: Optional[str] = None
    purchased_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    merchant: Optional[Dict[str, Any]] = None
    custom_fields: List[Dict[str, Any]] = Field(default_factory=list)


class BrexDepartment(_BrexModel):
    id: str
    name: Optional[str] = None
    parent_id: Optional[str] = None


class BrexLocation(_BrexModel):
    id: str
    name: Optional[str] = None
    address: Optional[Dict[str, Any]] = None


class BrexVendor(_BrexModel):
    id: str
    company_name: Optional[str] = None
    email: Optional[str] = None
    status: Optional[str] = None


class BrexReceipt(_BrexModel):
    id: str
    expense_id: Optional[str] = None
    url: Optional[str] = None
    file_name: Optional[str] = None


class BrexBudget(_BrexModel):
    id: str
    name: Optional[str] = None
    limit: Optional[Amount] = None
    period_type: Optional[str] = None


class BrexSpendLimit(_BrexModel):
    id: str
    name: Optional[str] = None
    limit: Optional[Amount] = None
    period_type: Optional[str] = None


class PageResult(_BrexModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_cursor: Optional[str] = None
