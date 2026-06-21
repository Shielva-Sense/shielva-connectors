"""Pydantic request/response schemas for the Adobe Sign REST v6 API.

camelCase aliases match Adobe's wire format; the connector boundary uses
plain ``Dict[str, Any]`` payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _AdobeSignModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class FileInfo(_AdobeSignModel):
    """One file entry inside an agreement's ``fileInfos`` list."""

    transient_document_id: Optional[str] = Field(default=None, alias="transientDocumentId")
    library_document_id: Optional[str] = Field(default=None, alias="libraryDocumentId")
    document_url: Optional[str] = Field(default=None, alias="documentURL")
    label: Optional[str] = None


class MemberInfo(_AdobeSignModel):
    email: str


class ParticipantSetInfo(_AdobeSignModel):
    member_infos: List[MemberInfo] = Field(default_factory=list, alias="memberInfos")
    order: int = 1
    role: str = "SIGNER"


class AgreementCreateRequest(_AdobeSignModel):
    file_infos: List[FileInfo] = Field(default_factory=list, alias="fileInfos")
    participant_sets_info: List[ParticipantSetInfo] = Field(
        default_factory=list,
        alias="participantSetsInfo",
    )
    name: str = ""
    signature_type: str = Field(default="ESIGN", alias="signatureType")
    state: str = "IN_PROCESS"
    message: Optional[str] = None


class AgreementResponse(_AdobeSignModel):
    agreement_id: str = Field(alias="id")
    name: Optional[str] = None
    status: Optional[str] = None
    type: Optional[str] = None
    created_date: Optional[datetime] = Field(default=None, alias="createdDate")
    expiration_time: Optional[datetime] = Field(default=None, alias="expirationTime")
    sender_email: Optional[str] = Field(default=None, alias="senderEmail")
    participant_sets_info: List[Dict[str, Any]] = Field(
        default_factory=list,
        alias="participantSetsInfo",
    )


class WebhookCreateRequest(_AdobeSignModel):
    name: str
    scope: str = "ACCOUNT"
    state: str = "ACTIVE"
    webhook_subscription_events: List[str] = Field(
        default_factory=list,
        alias="webhookSubscriptionEvents",
    )
    webhook_url_info: Dict[str, Any] = Field(default_factory=dict, alias="webhookUrlInfo")


class ReminderRequest(_AdobeSignModel):
    recipient_participant_ids: List[str] = Field(
        default_factory=list,
        alias="recipientParticipantIds",
    )
    status: str = "ACTIVE"
    note: Optional[str] = None


class BaseUrisResponse(_AdobeSignModel):
    api_access_point: Optional[str] = Field(default=None, alias="apiAccessPoint")
    web_access_point: Optional[str] = Field(default=None, alias="webAccessPoint")


class PageResult(_AdobeSignModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_cursor: Optional[str] = Field(default=None, alias="nextCursor")
