"""Pydantic request/response schemas for the Bandwidth APIs.

camelCase aliases match Bandwidth's wire format; the connector exposes
snake_case at its boundary and serialises via `by_alias=True`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _BandwidthModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class SendMessageRequest(_BandwidthModel):
    application_id: str = Field(alias="applicationId")
    to: List[str]
    from_: str = Field(alias="from")
    text: Optional[str] = None
    media: Optional[List[str]] = None
    tag: Optional[str] = None


class MessageResponse(_BandwidthModel):
    id: str
    owner: Optional[str] = None
    application_id: Optional[str] = Field(default=None, alias="applicationId")
    to: List[str] = Field(default_factory=list)
    from_: Optional[str] = Field(default=None, alias="from")
    text: Optional[str] = None
    media: Optional[List[str]] = None
    time: Optional[datetime] = None
    direction: Optional[str] = None
    segment_count: Optional[int] = Field(default=None, alias="segmentCount")


class CreateCallRequest(_BandwidthModel):
    application_id: str = Field(alias="applicationId")
    to: str
    from_: str = Field(alias="from")
    answer_url: str = Field(alias="answerUrl")
    answer_method: Optional[str] = Field(default="POST", alias="answerMethod")
    disconnect_url: Optional[str] = Field(default=None, alias="disconnectUrl")
    tag: Optional[str] = None


class CallResponse(_BandwidthModel):
    call_id: str = Field(alias="callId")
    account_id: Optional[str] = Field(default=None, alias="accountId")
    application_id: Optional[str] = Field(default=None, alias="applicationId")
    to: Optional[str] = None
    from_: Optional[str] = Field(default=None, alias="from")
    direction: Optional[str] = None
    state: Optional[str] = None
    start_time: Optional[datetime] = Field(default=None, alias="startTime")
    end_time: Optional[datetime] = Field(default=None, alias="endTime")
    answer_time: Optional[datetime] = Field(default=None, alias="answerTime")


class UpdateCallRequest(_BandwidthModel):
    state: Optional[str] = None
    redirect_url: Optional[str] = Field(default=None, alias="redirectUrl")
    redirect_method: Optional[str] = Field(default=None, alias="redirectMethod")


class RecordingResponse(_BandwidthModel):
    recording_id: str = Field(alias="recordingId")
    call_id: Optional[str] = Field(default=None, alias="callId")
    duration: Optional[str] = None
    channels: Optional[int] = None
    media_url: Optional[str] = Field(default=None, alias="mediaUrl")
    start_time: Optional[datetime] = Field(default=None, alias="startTime")
    end_time: Optional[datetime] = Field(default=None, alias="endTime")


class ApplicationResponse(_BandwidthModel):
    application_id: str = Field(alias="applicationId")
    service_type: Optional[str] = Field(default=None, alias="serviceType")
    app_name: Optional[str] = Field(default=None, alias="appName")


class PageResult(_BandwidthModel):
    """Generic page envelope for cursor-paginated Bandwidth endpoints."""

    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_page_token: Optional[str] = None
