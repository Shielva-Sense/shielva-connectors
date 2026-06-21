"""Pydantic + dataclass schemas for Bill.com REST API.

camelCase aliases match the Bill.com wire format; the connector boundary uses
``Dict[str, Any]`` payloads — these models are documentation + type checks
for consumers who want stronger typing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _BillcomModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


# ── Auth wire types ─────────────────────────────────────────────────────────


class LoginRequest(_BillcomModel):
    user_name: str = Field(alias="userName")
    password: str
    org_id: str = Field(alias="orgId")
    dev_key: str = Field(alias="devKey")


class LoginResponse(_BillcomModel):
    session_id: str = Field(alias="sessionId")
    user_id: Optional[str] = Field(default=None, alias="userId")
    organization_id: Optional[str] = Field(default=None, alias="organizationId")


# ── Resource wire types ─────────────────────────────────────────────────────


class Vendor(_BillcomModel):
    id: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    address1: Optional[str] = None
    address_city: Optional[str] = Field(default=None, alias="addressCity")
    address_state: Optional[str] = Field(default=None, alias="addressState")
    address_zip: Optional[str] = Field(default=None, alias="addressZip")
    address_country: Optional[str] = Field(default=None, alias="addressCountry")
    is_active: Optional[str] = Field(default=None, alias="isActive")


class Customer(_BillcomModel):
    id: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    bill_address1: Optional[str] = Field(default=None, alias="billAddress1")


class BillLineItem(_BillcomModel):
    amount: float
    chart_of_account_id: Optional[str] = Field(default=None, alias="chartOfAccountId")
    description: Optional[str] = None


class Bill(_BillcomModel):
    id: Optional[str] = None
    vendor_id: str = Field(alias="vendorId")
    invoice_number: str = Field(alias="invoiceNumber")
    invoice_date: str = Field(alias="invoiceDate")
    due_date: str = Field(alias="dueDate")
    amount: float
    line_items: List[BillLineItem] = Field(default_factory=list, alias="billLineItems")
    payment_status: Optional[str] = Field(default=None, alias="paymentStatus")


class InvoiceLineItem(_BillcomModel):
    amount: float
    item_id: Optional[str] = Field(default=None, alias="itemId")
    description: Optional[str] = None
    quantity: Optional[float] = None


class Invoice(_BillcomModel):
    id: Optional[str] = None
    customer_id: str = Field(alias="customerId")
    invoice_number: str = Field(alias="invoiceNumber")
    invoice_date: str = Field(alias="invoiceDate")
    due_date: Optional[str] = Field(default=None, alias="dueDate")
    amount: float
    line_items: List[InvoiceLineItem] = Field(default_factory=list, alias="invoiceLineItems")


class Payment(_BillcomModel):
    id: Optional[str] = None
    bill_id: Optional[str] = Field(default=None, alias="billId")
    process_date: Optional[str] = Field(default=None, alias="processDate")
    amount: Optional[float] = None
    status: Optional[str] = None


class ChartOfAccount(_BillcomModel):
    id: Optional[str] = None
    name: Optional[str] = None
    account_type: Optional[str] = Field(default=None, alias="accountType")
    is_active: Optional[str] = Field(default=None, alias="isActive")


class Classification(_BillcomModel):
    id: Optional[str] = None
    name: Optional[str] = None
    is_active: Optional[str] = Field(default=None, alias="isActive")


class Location(_BillcomModel):
    id: Optional[str] = None
    name: Optional[str] = None
    is_active: Optional[str] = Field(default=None, alias="isActive")


# ── Envelope ────────────────────────────────────────────────────────────────


@dataclass
class BillcomEnvelope:
    """Bill.com response wrapper.

    response_status == 0 → ``response_data`` is the payload (dict or list).
    response_status == 1 → ``response_data`` is ``{error_code, error_message}``.
    """

    response_status: int
    response_message: str
    response_data: Any = None


@dataclass
class ListPage:
    """Generic paginated result shape for ``List/*.json`` endpoints."""

    items: List[Dict[str, Any]] = field(default_factory=list)
    start: int = 0
    max: int = 99
