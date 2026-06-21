"""Pydantic request/response schemas for the Mercury REST API.

camelCase aliases match Mercury wire format; the connector boundary uses
Dict[str, Any] payloads but these schemas are the source of truth for the
shape of money-movement requests + paginated responses.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _MercuryModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


# ── Request schemas ─────────────────────────────────────────────────────────


class SendPaymentRequest(_MercuryModel):
    """Body for POST /account/{id}/transactions."""

    recipient_id: str = Field(alias="recipientId")
    amount: float
    payment_method: str = Field(alias="paymentMethod")
    note: Optional[str] = None
    external_memo: Optional[str] = Field(default=None, alias="externalMemo")


class CreateRecipientRequest(_MercuryModel):
    """Body for POST /recipient."""

    name: str
    emails: List[str] = Field(default_factory=list)
    default_payment_method: Optional[str] = Field(
        default=None, alias="defaultPaymentMethod"
    )
    payment_methods: List[Dict[str, Any]] = Field(
        default_factory=list, alias="paymentMethods"
    )


# ── Response schemas ────────────────────────────────────────────────────────


class AccountResponse(_MercuryModel):
    account_id: str = Field(alias="id")
    name: Optional[str] = None
    nickname: Optional[str] = None
    kind: Optional[str] = None
    status: Optional[str] = None
    type: Optional[str] = None
    available_balance: Optional[float] = Field(default=None, alias="availableBalance")
    current_balance: Optional[float] = Field(default=None, alias="currentBalance")
    routing_number: Optional[str] = Field(default=None, alias="routingNumber")
    account_number: Optional[str] = Field(default=None, alias="accountNumber")


class TransactionResponse(_MercuryModel):
    transaction_id: str = Field(alias="id")
    account_id: Optional[str] = Field(default=None, alias="accountId")
    amount: float = 0.0
    status: Optional[str] = None
    kind: Optional[str] = None
    counterparty_name: Optional[str] = Field(default=None, alias="counterpartyName")
    counterparty_id: Optional[str] = Field(default=None, alias="counterpartyId")
    posted_at: Optional[datetime] = Field(default=None, alias="postedAt")
    created_at: Optional[datetime] = Field(default=None, alias="createdAt")
    note: Optional[str] = None
    external_memo: Optional[str] = Field(default=None, alias="externalMemo")


class RecipientResponse(_MercuryModel):
    recipient_id: str = Field(alias="id")
    name: str = ""
    emails: List[str] = Field(default_factory=list)
    default_payment_method: Optional[str] = Field(
        default=None, alias="defaultPaymentMethod"
    )
    payment_methods: List[Dict[str, Any]] = Field(
        default_factory=list, alias="paymentMethods"
    )
    nickname: Optional[str] = None
    status: Optional[str] = None


class StatementResponse(_MercuryModel):
    account_id: str
    start_date: str
    end_date: str
    items: List[Dict[str, Any]] = Field(default_factory=list)
