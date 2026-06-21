"""Local dataclasses for the Personio connector.

Includes lightweight `@property` shims so callers can read `.auth_status`
(maps to shared `AuthStatus`) and `.health` (maps to shared `ConnectorHealth`)
on internal status payloads without round-tripping through the platform-wide
`ConnectorStatus` dataclass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.base_connector import AuthStatus, ConnectorHealth


@dataclass
class PersonioEmployee:
    """A single Personio employee record (subset of attributes)."""

    employee_id: int
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    status: str = ""
    department: str = ""
    position: str = ""
    hire_date: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PersonioTimeOff:
    """A Personio time-off record."""

    time_off_id: int
    employee_id: int
    start_date: str
    end_date: str
    time_off_type_id: int = 0
    half_day_start: bool = False
    half_day_end: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PersonioAttendance:
    """A Personio attendance entry."""

    attendance_id: int
    employee_id: int
    date: str
    start_time: str
    end_time: str
    break_time: int = 0
    comment: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PersonioApplicant:
    """A Personio recruitment applicant."""

    applicant_id: int
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    job_position: str = ""
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PersonioConnectorStatus:
    """Internal status payload with shims onto shared enums."""

    connector_id: str
    _health: ConnectorHealth
    _auth_status: AuthStatus
    message: str = ""

    @property
    def health(self) -> ConnectorHealth:
        return self._health

    @property
    def auth_status(self) -> AuthStatus:
        return self._auth_status


@dataclass
class PersonioListResponse:
    """Generic Personio list-response wrapper."""

    items: List[Dict[str, Any]] = field(default_factory=list)
    limit: int = 0
    offset: int = 0
    total: Optional[int] = None
