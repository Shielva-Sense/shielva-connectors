"""Local dataclass shapes for the JazzHR connector.

These exist purely as type hints / serialisation helpers. The connector
boundary speaks `Dict[str, Any]` to keep parity with the raw JazzHR wire
format (snake_case keys). All public connector methods return raw dicts /
lists — these dataclasses are NOT used at the boundary.

The shared `AuthStatus` and `ConnectorHealth` enums are re-exported here for
import convenience (`from models import AuthStatus, ConnectorHealth`).
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.base_connector import AuthStatus, ConnectorHealth  # noqa: F401  (re-export)


@dataclass
class JazzHRJob:
    """Local representation of a JazzHR job posting."""

    id: str
    title: str
    status: Optional[str] = None
    department: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country_id: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    hiring_lead_id: Optional[str] = None
    board_code: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JazzHRApplicant:
    """Local representation of a JazzHR applicant."""

    id: str
    first_name: str
    last_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country_id: Optional[str] = None
    apply_date: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JazzHRNote:
    """Local representation of a JazzHR applicant note."""

    id: str
    contents: str
    security: str = "public"
    user_id: Optional[str] = None
    applicant_id: Optional[str] = None
    created_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JazzHRWorkflowStep:
    """Local representation of a JazzHR workflow step."""

    id: str
    name: str
    order: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ListResponse:
    """Generic paginated list response shape."""

    items: List[Dict[str, Any]] = field(default_factory=list)
    page: int = 1
    has_more: bool = False
