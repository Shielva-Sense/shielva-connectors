"""Pydantic schemas for Wave GraphQL request/response shapes.

camelCase aliases match Wave wire format; the connector boundary uses
`Dict[str, Any]` payloads — these models are typing-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _WaveModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_WaveModel):
    """Wave's standard `(page, pageSize)` paging input."""
    page: int = 1
    page_size: int = Field(default=50, alias="pageSize")


class PageInfo(_WaveModel):
    """Wave's standard `pageInfo` envelope on connection types."""
    current_page: Optional[int] = Field(default=None, alias="currentPage")
    total_pages: Optional[int] = Field(default=None, alias="totalPages")
    total_count: Optional[int] = Field(default=None, alias="totalCount")


class Money(_WaveModel):
    value: Optional[str] = None
    currency: Optional[Dict[str, Any]] = None


class CustomerResponse(_WaveModel):
    customer_id: str = Field(alias="id")
    name: Optional[str] = None
    email: Optional[str] = None
    first_name: Optional[str] = Field(default=None, alias="firstName")
    last_name: Optional[str] = Field(default=None, alias="lastName")


class InvoiceResponse(_WaveModel):
    invoice_id: str = Field(alias="id")
    invoice_number: Optional[str] = Field(default=None, alias="invoiceNumber")
    status: Optional[str] = None
    invoice_date: Optional[datetime] = Field(default=None, alias="invoiceDate")
    due_date: Optional[datetime] = Field(default=None, alias="dueDate")
    total: Optional[Money] = None
    customer: Optional[Dict[str, Any]] = None


class ProductResponse(_WaveModel):
    product_id: str = Field(alias="id")
    name: Optional[str] = None
    description: Optional[str] = None
    unit_price: Optional[str] = Field(default=None, alias="unitPrice")
    is_sold: Optional[bool] = Field(default=None, alias="isSold")
    is_bought: Optional[bool] = Field(default=None, alias="isBought")


class BusinessResponse(_WaveModel):
    business_id: str = Field(alias="id")
    name: Optional[str] = None
    timezone: Optional[str] = None
    currency: Optional[Dict[str, Any]] = None
    address: Optional[Dict[str, Any]] = None


class GraphQLError(_WaveModel):
    message: str
    path: List[Any] = Field(default_factory=list)
    extensions: Dict[str, Any] = Field(default_factory=dict)


class PageResult(_WaveModel):
    """Connection-style result envelope."""
    page_info: Optional[PageInfo] = Field(default=None, alias="pageInfo")
    edges: List[Dict[str, Any]] = Field(default_factory=list)
