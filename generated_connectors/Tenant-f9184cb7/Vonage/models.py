"""Pydantic request/response schemas for the Vonage APIs.

snake_case is exposed at the connector boundary; camelCase aliases match
Vonage's wire format where it differs. Serialise with `by_alias=True`.

These schemas are reference shapes — the connector accepts plain dicts
on the public API (mirroring Bandwidth/Wix). They are exported so that
callers wishing to validate payloads in their own code can opt in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _VonageModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


# ── SMS ──────────────────────────────────────────────────────────────────────


class SendSMSRequest(_VonageModel):
    from_: str = Field(alias="from")
    to: str
    text: str
    type_: Optional[str] = Field(default="text", alias="type")
    callback: Optional[str] = None
    status_report_req: Optional[int] = Field(default=None, alias="status-report-req")


class SMSEnvelopeItem(_VonageModel):
    to: Optional[str] = None
    message_id: Optional[str] = Field(default=None, alias="message-id")
    status: Optional[str] = None
    remaining_balance: Optional[str] = Field(default=None, alias="remaining-balance")
    message_price: Optional[str] = Field(default=None, alias="message-price")
    network: Optional[str] = None
    error_text: Optional[str] = Field(default=None, alias="error-text")


class SendSMSResponse(_VonageModel):
    message_count: Optional[int] = Field(default=None, alias="message-count")
    messages: List[SMSEnvelopeItem] = Field(default_factory=list)


# ── Voice ────────────────────────────────────────────────────────────────────


class CallEndpoint(_VonageModel):
    type: str = "phone"
    number: Optional[str] = None
    user: Optional[str] = None


class CreateCallRequest(_VonageModel):
    to: List[CallEndpoint]
    from_: CallEndpoint = Field(alias="from")
    ncco: Optional[List[Dict[str, Any]]] = None
    answer_url: Optional[List[str]] = None
    answer_method: Optional[str] = "GET"
    event_url: Optional[List[str]] = None
    event_method: Optional[str] = "POST"
    machine_detection: Optional[str] = None
    length_timer: Optional[int] = None
    ringing_timer: Optional[int] = None


class CallResponse(_VonageModel):
    uuid: Optional[str] = None
    conversation_uuid: Optional[str] = None
    direction: Optional[str] = None
    status: Optional[str] = None
    rate: Optional[str] = None
    price: Optional[str] = None
    duration: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    network: Optional[str] = None
    from_: Optional[Dict[str, Any]] = Field(default=None, alias="from")
    to: Optional[Dict[str, Any]] = None


class UpdateCallRequest(_VonageModel):
    action: str  # "hangup" | "mute" | "unmute" | "earmuff" | "unearmuff" | "transfer"
    destination: Optional[Dict[str, Any]] = None


# ── Verify v2 ────────────────────────────────────────────────────────────────


class VerifyWorkflow(_VonageModel):
    channel: str  # "sms" | "voice" | "email" | "whatsapp"
    to: str
    from_: Optional[str] = Field(default=None, alias="from")
    app_hash: Optional[str] = None


class SendVerifyRequest(_VonageModel):
    brand: str
    workflow: List[VerifyWorkflow]
    locale: Optional[str] = None
    channel_timeout: Optional[int] = None
    code_length: Optional[int] = None
    code: Optional[str] = None


class VerifyResponse(_VonageModel):
    request_id: Optional[str] = None
    check_url: Optional[str] = None


# ── Numbers ──────────────────────────────────────────────────────────────────


class NumberRecord(_VonageModel):
    country: Optional[str] = None
    msisdn: Optional[str] = None
    type: Optional[str] = None
    features: Optional[List[str]] = None
    cost: Optional[str] = None
    voice_callback_type: Optional[str] = None
    voice_callback_value: Optional[str] = None
    messages_callback_type: Optional[str] = None
    messages_callback_value: Optional[str] = None


class NumbersListResponse(_VonageModel):
    count: Optional[int] = None
    numbers: List[NumberRecord] = Field(default_factory=list)


# ── Applications ─────────────────────────────────────────────────────────────


class ApplicationResponse(_VonageModel):
    id: Optional[str] = None
    name: Optional[str] = None
    capabilities: Optional[Dict[str, Any]] = None
    keys: Optional[Dict[str, Any]] = None


# ── Generic pagination envelope ──────────────────────────────────────────────


class PageResult(_VonageModel):
    """Generic page envelope for cursor-paginated Vonage endpoints."""

    items: List[Dict[str, Any]] = Field(default_factory=list)
    count: Optional[int] = None
    next_url: Optional[str] = None
