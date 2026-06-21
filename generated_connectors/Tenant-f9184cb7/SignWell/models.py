"""Pydantic request/response schemas for SignWell REST APIs.

snake_case in both directions (SignWell uses snake_case on the wire). The
connector boundary uses `Dict[str, Any]` payloads for forward-compatibility
with new SignWell fields; these models are typed read helpers for callers
that want stronger guarantees.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _SignWellModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


# ── Recipients & fields ────────────────────────────────────────────────────


class Recipient(_SignWellModel):
    """A signer / CC recipient on a SignWell document."""

    id: Optional[str] = None
    name: str
    email: str
    order: int = 1
    subject: Optional[str] = None
    message: Optional[str] = None
    status: Optional[str] = None


class TemplateField(_SignWellModel):
    """A pre-filled field on a SignWell template-document."""

    api_id: str
    value: Any = None


# ── Request bodies ─────────────────────────────────────────────────────────


class CreateDocumentRequest(_SignWellModel):
    name: str
    recipients: List[Dict[str, Any]] = Field(default_factory=list)
    files: List[Dict[str, Any]] = Field(default_factory=list)
    file_urls: List[str] = Field(default_factory=list)
    message: Optional[str] = None
    subject: Optional[str] = None
    test_mode: bool = True
    draft: bool = False
    embedded_signing: bool = False
    expires_in: Optional[int] = None
    reminders: bool = True


class CreateDocumentFromTemplateRequest(_SignWellModel):
    template_id: str
    name: str
    recipients: List[Dict[str, Any]] = Field(default_factory=list)
    template_fields: List[Dict[str, Any]] = Field(default_factory=list)
    test_mode: bool = True
    draft: bool = False
    embedded_signing: bool = False


class CreateWebhookRequest(_SignWellModel):
    url: str
    events: List[str] = Field(default_factory=list)


# ── Response projections ───────────────────────────────────────────────────


class SignWellDocument(_SignWellModel):
    """Lightweight projection of a SignWell document response."""

    id: str
    name: Optional[str] = None
    status: Optional[str] = None
    test_mode: Optional[bool] = None
    embedded_signing: Optional[bool] = None
    subject: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    recipients: List[Recipient] = Field(default_factory=list)
    files: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SignWellTemplate(_SignWellModel):
    """Lightweight projection of a SignWell template response."""

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    fields: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PageResult(_SignWellModel):
    """Generic page envelope (documents / templates)."""

    items: List[Dict[str, Any]] = Field(default_factory=list)
    page: Optional[int] = None
    next_page: Optional[int] = None
    has_more: Optional[bool] = None
