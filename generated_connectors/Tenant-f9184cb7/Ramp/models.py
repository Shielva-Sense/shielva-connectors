"""Pydantic request/response schemas for Ramp Developer API.

snake_case mirrors Ramp wire format; the connector boundary uses
Dict[str, Any] payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _RampModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Page(_RampModel):
    next: Optional[str] = None


class UserResponse(_RampModel):
    user_id: str = Field(alias="id")
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: Optional[str] = None
    department_id: Optional[str] = None
    location_id: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None


class CardResponse(_RampModel):
    card_id: str = Field(alias="id")
    display_name: Optional[str] = None
    user_id: Optional[str] = None
    is_physical: Optional[bool] = None
    state: Optional[str] = None
    spending_restrictions: Optional[Dict[str, Any]] = None


class TransactionResponse(_RampModel):
    transaction_id: str = Field(alias="id")
    amount: Optional[float] = None
    currency_code: Optional[str] = None
    merchant_name: Optional[str] = None
    user_id: Optional[str] = None
    card_id: Optional[str] = None
    sk_category_id: Optional[str] = None
    user_transaction_time: Optional[str] = None


class PageResult(_RampModel):
    data: List[Dict[str, Any]] = Field(default_factory=list)
    page: Optional[Page] = None
