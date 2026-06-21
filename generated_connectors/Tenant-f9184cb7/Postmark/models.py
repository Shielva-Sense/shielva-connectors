"""Local dataclasses + property shims for the Postmark connector.

The Shielva SDK ships its own ``AuthStatus`` / ``ConnectorHealth`` /
``ConnectorStatus`` / ``TokenInfo`` enums. We re-export the SDK types here so
call sites can ``from models import …`` without a second import path, and add
a few connector-local dataclasses for Postmark-specific payloads (send results,
message envelopes, bounces).

The ``@property`` shims allow tests and gateway code to read fields by either
the Postmark API casing (``MessageID``, ``SubmittedAt``) OR snake_case
(``message_id``, ``submitted_at``) without forcing one convention onto the wire
format.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# Re-export SDK status enums so consumers can `from models import AuthStatus` etc.
from shared.base_connector import (  # noqa: F401  (re-exported)
    AuthStatus,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)


# ── Pydantic request/response schemas ──────────────────────────────────────


class _PostmarkModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class SendEmailRequest(_PostmarkModel):
    """Body for POST /email — Postmark's PascalCase wire format."""

    From: str
    To: str
    Subject: str
    HtmlBody: Optional[str] = None
    TextBody: Optional[str] = None
    Cc: Optional[str] = None
    Bcc: Optional[str] = None
    Tag: Optional[str] = None
    Metadata: Optional[Dict[str, Any]] = None
    MessageStream: str = "outbound"


class SendTemplateRequest(_PostmarkModel):
    """Body for POST /email/withTemplate."""

    From: str
    To: str
    TemplateModel: Dict[str, Any] = Field(default_factory=dict)
    MessageStream: str = "outbound"
    TemplateId: Optional[int] = None
    TemplateAlias: Optional[str] = None


class MessageListResponse(_PostmarkModel):
    """Response for GET /messages/outbound."""

    TotalCount: int = 0
    Messages: List[Dict[str, Any]] = Field(default_factory=list)


class BounceListResponse(_PostmarkModel):
    """Response for GET /bounces."""

    TotalCount: int = 0
    Bounces: List[Dict[str, Any]] = Field(default_factory=list)


class TemplateListResponse(_PostmarkModel):
    """Response for GET /templates."""

    TotalCount: int = 0
    Templates: List[Dict[str, Any]] = Field(default_factory=list)


# ── Dataclass shims with PascalCase + snake_case property bridge ──────────


@dataclass
class PostmarkSendResult:
    """Parsed response from POST /email and POST /email/withTemplate."""

    To: str = ""
    SubmittedAt: str = ""
    MessageID: str = ""
    ErrorCode: int = 0
    Message: str = ""

    @property
    def to(self) -> str:
        return self.To

    @property
    def submitted_at(self) -> str:
        return self.SubmittedAt

    @property
    def message_id(self) -> str:
        return self.MessageID

    @property
    def error_code(self) -> int:
        return self.ErrorCode

    @property
    def message(self) -> str:
        return self.Message

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PostmarkSendResult":
        return cls(
            To=str(data.get("To", "")),
            SubmittedAt=str(data.get("SubmittedAt", "")),
            MessageID=str(data.get("MessageID", "")),
            ErrorCode=int(data.get("ErrorCode", 0)),
            Message=str(data.get("Message", "")),
        )


@dataclass
class PostmarkServerInfo:
    """Parsed response from GET /server."""

    ID: int = 0
    Name: str = ""
    ApiTokens: List[str] = field(default_factory=list)
    Color: str = ""
    ServerLink: str = ""

    @property
    def id(self) -> int:
        return self.ID

    @property
    def name(self) -> str:
        return self.Name

    @property
    def api_tokens(self) -> List[str]:
        return self.ApiTokens

    @property
    def server_link(self) -> str:
        return self.ServerLink

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PostmarkServerInfo":
        return cls(
            ID=int(data.get("ID", 0)),
            Name=str(data.get("Name", "")),
            ApiTokens=list(data.get("ApiTokens", []) or []),
            Color=str(data.get("Color", "")),
            ServerLink=str(data.get("ServerLink", "")),
        )


@dataclass
class PostmarkBounce:
    """Parsed response from GET /bounces/{id}."""

    ID: int = 0
    Type: str = ""
    TypeCode: int = 0
    Description: str = ""
    Email: str = ""
    Inactive: bool = False
    CanActivate: bool = False
    Subject: str = ""
    BouncedAt: Optional[datetime] = None

    @property
    def id(self) -> int:
        return self.ID

    @property
    def type(self) -> str:
        return self.Type

    @property
    def email(self) -> str:
        return self.Email

    @property
    def inactive(self) -> bool:
        return self.Inactive

    @property
    def can_activate(self) -> bool:
        return self.CanActivate

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PostmarkBounce":
        return cls(
            ID=int(data.get("ID", 0)),
            Type=str(data.get("Type", "")),
            TypeCode=int(data.get("TypeCode", 0)),
            Description=str(data.get("Description", "")),
            Email=str(data.get("Email", "")),
            Inactive=bool(data.get("Inactive", False)),
            CanActivate=bool(data.get("CanActivate", False)),
            Subject=str(data.get("Subject", "")),
        )


__all__ = [
    # SDK re-exports
    "AuthStatus",
    "ConnectorHealth",
    "ConnectorStatus",
    "NormalizedDocument",
    "SyncResult",
    "SyncStatus",
    "TokenInfo",
    # Pydantic request/response schemas
    "SendEmailRequest",
    "SendTemplateRequest",
    "MessageListResponse",
    "BounceListResponse",
    "TemplateListResponse",
    # Connector-local dataclasses
    "PostmarkSendResult",
    "PostmarkServerInfo",
    "PostmarkBounce",
]
