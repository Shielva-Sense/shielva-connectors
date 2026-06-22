"""Pydantic request / response schemas for Crisp REST APIs.

Crisp wire format is snake_case; the message-send body uses the reserved word
`from` which we expose in Python as `from_`. Models below stay close to the
wire so callers can pass dicts through without conversion.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _CrispModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class MessageRequest(_CrispModel):
    """Outgoing message body — `from_` is renamed to `from` on the wire."""

    type: str
    from_: str = Field(alias="from")
    origin: str
    content: Any

    def to_wire(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "from": self.from_,
            "origin": self.origin,
            "content": self.content,
        }


class CreatePersonRequest(_CrispModel):
    """`/website/{id}/people/profile` body — keys omitted when None."""

    email: Optional[str] = None
    person: Optional[Dict[str, Any]] = None
    segments: Optional[List[str]] = None

    def to_wire(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if self.email is not None:
            body["email"] = self.email
        if self.person is not None:
            body["person"] = self.person
        if self.segments is not None:
            body["segments"] = self.segments
        return body


class ConversationResponse(_CrispModel):
    session_id: str
    website_id: Optional[str] = None
    state: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    last_message: Optional[str] = None
    preview: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class PersonResponse(_CrispModel):
    people_id: str
    email: Optional[str] = None
    person: Optional[Dict[str, Any]] = None
    segments: List[str] = Field(default_factory=list)
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class HelpdeskArticleResponse(_CrispModel):
    article_id: str
    title: Optional[str] = None
    content: Optional[str] = None
    locale: Optional[str] = None
    category_id: Optional[str] = None
    visibility: Optional[str] = None
    url: Optional[str] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class PageResult(_CrispModel):
    """Generic paginated envelope."""

    data: List[Dict[str, Any]] = Field(default_factory=list)
    error: bool = False
    reason: Optional[str] = None
    next_page: Optional[int] = None
