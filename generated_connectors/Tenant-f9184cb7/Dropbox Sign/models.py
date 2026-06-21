"""Pydantic request/response schemas for the Dropbox Sign (HelloSign) REST API.

The connector boundary still uses `Dict[str, Any]` for raw payloads — these
models exist as typed read helpers for downstream consumers that want to
introspect the most common fields without re-deriving them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _DropboxSignModel(BaseModel):
    """Shared base — allows extra fields (the API is evolving)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Signer(_DropboxSignModel):
    """A single signer on a signature request."""

    name: str
    email_address: str = Field(alias="email_address")
    order: Optional[int] = None
    role: Optional[str] = None  # used by send_with_template


class Signature(_DropboxSignModel):
    """A signature slot returned inside a signature_request payload."""

    signature_id: Optional[str] = None
    signer_email_address: Optional[str] = None
    signer_name: Optional[str] = None
    status_code: Optional[str] = None
    signed_at: Optional[datetime] = None
    last_viewed_at: Optional[datetime] = None
    last_reminded_at: Optional[datetime] = None


class SignatureRequest(_DropboxSignModel):
    """A Dropbox Sign signature_request resource."""

    signature_request_id: str
    title: Optional[str] = None
    subject: Optional[str] = None
    message: Optional[str] = None
    is_complete: bool = False
    is_declined: bool = False
    has_error: bool = False
    requester_email_address: Optional[str] = None
    signing_url: Optional[str] = None
    details_url: Optional[str] = None
    signatures: List[Signature] = Field(default_factory=list)
    created_at: Optional[datetime] = None

    @property
    def status(self) -> str:
        if self.is_declined:
            return "declined"
        if self.has_error:
            return "error"
        if self.is_complete:
            return "completed"
        return "pending"


class Template(_DropboxSignModel):
    """A Dropbox Sign template resource."""

    template_id: str
    title: Optional[str] = None
    message: Optional[str] = None
    can_edit: bool = False
    is_locked: bool = False
    signer_roles: List[Dict[str, Any]] = Field(default_factory=list)

    @property
    def role_names(self) -> List[str]:
        return [r.get("name", "") for r in self.signer_roles if r.get("name")]


class ListInfo(_DropboxSignModel):
    """Pagination envelope used by every `*_list` endpoint."""

    page: int = 1
    num_pages: int = 1
    num_results: int = 0
    page_size: int = 20
