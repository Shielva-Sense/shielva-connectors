"""Local dataclasses + AuthStatus / ConnectorHealth re-export shims for the Aircall connector.

These mirror the shared SDK enums so callers that don't already depend on
shared.base_connector can still import status types from one place.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Re-export shims so callers that don't already depend on shared.base_connector
# can import these from one place. Enums cannot be subclassed in Python, so we
# use direct re-export bindings.
from shared.base_connector import AuthStatus, ConnectorHealth  # noqa: F401


@dataclass
class AircallUser:
    """Aircall user (agent)."""
    id: int
    direct_link: str = ""
    name: str = ""
    email: str = ""
    available: bool = False
    availability_status: str = ""
    time_zone: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.email or str(self.id)


@dataclass
class AircallNumber:
    """Aircall phone number assigned to a team or user."""
    id: int
    direct_link: str = ""
    name: str = ""
    digits: str = ""
    country: str = ""
    is_ivr: bool = False

    @property
    def e164(self) -> str:
        return self.digits


@dataclass
class AircallCall:
    """Aircall call event."""
    id: int
    direct_link: str = ""
    direction: str = ""           # inbound | outbound
    status: str = ""              # initial | answered | done
    missed_call_reason: str = ""
    started_at: Optional[int] = None
    answered_at: Optional[int] = None
    ended_at: Optional[int] = None
    duration: int = 0
    voicemail: Optional[str] = None
    recording: Optional[str] = None
    raw_digits: str = ""
    user_id: Optional[int] = None
    number_id: Optional[int] = None
    contact_id: Optional[int] = None

    @property
    def is_inbound(self) -> bool:
        return self.direction == "inbound"


@dataclass
class AircallContact:
    """Aircall CRM contact."""
    id: int
    direct_link: str = ""
    first_name: str = ""
    last_name: str = ""
    company_name: str = ""
    phone_numbers: List[Dict[str, Any]] = field(default_factory=list)
    emails: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or self.company_name or str(self.id)


@dataclass
class CreateContactRequest:
    """Payload for POST /contacts."""
    first_name: str
    last_name: str
    company_name: Optional[str] = None
    phone_numbers: Optional[List[Dict[str, str]]] = None
    emails: Optional[List[Dict[str, str]]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "first_name": self.first_name,
            "last_name": self.last_name,
        }
        if self.company_name:
            payload["company_name"] = self.company_name
        if self.phone_numbers:
            payload["phone_numbers"] = self.phone_numbers
        if self.emails:
            payload["emails"] = self.emails
        return payload


@dataclass
class AircallTeam:
    """Aircall team — a group of agents."""
    id: int
    name: str = ""
    direct_link: str = ""
    users: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AircallTag:
    """Aircall call tag."""
    id: int
    name: str = ""
    color: str = ""
    description: str = ""


@dataclass
class AircallWebhook:
    """Aircall webhook subscription."""
    webhook_id: str
    direct_link: str = ""
    url: str = ""
    active: bool = True
    events: List[str] = field(default_factory=list)
    created_at: Optional[int] = None
