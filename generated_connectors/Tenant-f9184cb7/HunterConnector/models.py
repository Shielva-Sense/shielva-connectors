"""Dataclasses for the Hunter.io connector.

Hunter wire format is snake_case JSON wrapped in `{"data": {...}, "meta": {...}}`.
These dataclasses describe the connector boundary; the public methods return
raw `Dict[str, Any]` for forward compatibility with new Hunter fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DomainSearchRequest:
    domain: Optional[str] = None
    company: Optional[str] = None
    limit: int = 25
    offset: int = 0
    type: Optional[str] = None
    seniority: Optional[str] = None
    department: Optional[str] = None


@dataclass
class EmailFinderRequest:
    domain: Optional[str] = None
    company: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    max_duration: int = 10


@dataclass
class EmailVerifierRequest:
    email: str = ""


@dataclass
class CreateLeadRequest:
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    lead_list_id: Optional[int] = None
    source: Optional[str] = None


@dataclass
class CreateLeadListRequest:
    name: str = ""
    team_id: Optional[int] = None


@dataclass
class HunterAPIResponse:
    """Hunter envelope: `{"data": {...}, "meta": {...}}`."""

    data: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HunterLead:
    id: Optional[int] = None
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    position: Optional[str] = None
    phone_number: Optional[str] = None
    lead_list_id: Optional[int] = None
    source: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


__all__ = [
    "DomainSearchRequest",
    "EmailFinderRequest",
    "EmailVerifierRequest",
    "CreateLeadRequest",
    "CreateLeadListRequest",
    "HunterAPIResponse",
    "HunterLead",
]
