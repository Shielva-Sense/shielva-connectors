"""Pydantic request/response schemas for Wix REST APIs.

camelCase aliases match Wix wire format; the connector boundary uses
Dict[str, Any] payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _WixModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_WixModel):
    limit: int = 100
    cursor: Optional[str] = None


class QueryRequest(_WixModel):
    """Generic Wix Query API body."""
    filter: Dict[str, Any] = Field(default_factory=dict)
    paging: Paging = Field(default_factory=Paging)
    sort: List[Dict[str, str]] = Field(default_factory=list)


class MemberResponse(_WixModel):
    member_id: str = Field(alias="_id")
    login_email: Optional[str] = Field(default=None, alias="loginEmail")
    status: Optional[str] = None
    profile: Optional[Dict[str, Any]] = None
    contact: Optional[Dict[str, Any]] = None
    created_date: Optional[datetime] = Field(default=None, alias="_createdDate")
    updated_date: Optional[datetime] = Field(default=None, alias="_updatedDate")


class OrderResponse(_WixModel):
    order_id: str = Field(alias="id")
    number: Optional[str] = None
    status: Optional[str] = None
    fulfillment_status: Optional[str] = Field(default=None, alias="fulfillmentStatus")
    payment_status: Optional[str] = Field(default=None, alias="paymentStatus")
    created_date: Optional[datetime] = Field(default=None, alias="createdDate")
    totals: Optional[Dict[str, Any]] = None
    line_items: List[Dict[str, Any]] = Field(default_factory=list, alias="lineItems")
    billing_info: Optional[Dict[str, Any]] = Field(default=None, alias="billingInfo")


class FormSubmissionResponse(_WixModel):
    submission_id: str = Field(alias="_id")
    form_id: str = Field(alias="formId")
    submissions: Dict[str, Any] = Field(default_factory=dict)
    created_date: Optional[datetime] = Field(default=None, alias="_createdDate")


class PageResult(_WixModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_cursor: Optional[str] = None
