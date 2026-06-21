"""Local dataclasses + AuthStatus / ConnectorHealth shims for the Hunter connector.

The real enums live in shared.base_connector. We re-expose them here as
properties so callers can `from models import AuthStatus, ConnectorHealth`
without coupling against the SDK package path during static analysis.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.base_connector import (
    AuthStatus as _SDKAuthStatus,
    ConnectorHealth as _SDKConnectorHealth,
)


class _AuthStatusShim:
    """Property-based shim that proxies to the SDK AuthStatus enum."""

    @property
    def PENDING(self) -> "_SDKAuthStatus":
        return _SDKAuthStatus.PENDING

    @property
    def CONNECTED(self) -> "_SDKAuthStatus":
        return _SDKAuthStatus.CONNECTED

    @property
    def AUTHENTICATED(self) -> "_SDKAuthStatus":
        return _SDKAuthStatus.AUTHENTICATED

    @property
    def TOKEN_EXPIRED(self) -> "_SDKAuthStatus":
        return _SDKAuthStatus.TOKEN_EXPIRED

    @property
    def EXPIRED(self) -> "_SDKAuthStatus":
        return _SDKAuthStatus.EXPIRED

    @property
    def MISSING_CREDENTIALS(self) -> "_SDKAuthStatus":
        return _SDKAuthStatus.MISSING_CREDENTIALS


class _ConnectorHealthShim:
    """Property-based shim that proxies to the SDK ConnectorHealth enum."""

    @property
    def HEALTHY(self) -> "_SDKConnectorHealth":
        return _SDKConnectorHealth.HEALTHY

    @property
    def DEGRADED(self) -> "_SDKConnectorHealth":
        return _SDKConnectorHealth.DEGRADED

    @property
    def OFFLINE(self) -> "_SDKConnectorHealth":
        return _SDKConnectorHealth.OFFLINE


AuthStatus = _AuthStatusShim()
ConnectorHealth = _ConnectorHealthShim()


# ── Hunter request / response dataclasses ─────────────────────────────────


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
    """Generic Hunter.io envelope.

    Every Hunter response is `{ "data": {...}, "meta": {...} }`.
    """

    data: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HunterLead:
    id: Optional[int] = None
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    lead_list_id: Optional[int] = None
    source: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


__all__ = [
    "AuthStatus",
    "ConnectorHealth",
    "DomainSearchRequest",
    "EmailFinderRequest",
    "EmailVerifierRequest",
    "CreateLeadRequest",
    "CreateLeadListRequest",
    "HunterAPIResponse",
    "HunterLead",
]
