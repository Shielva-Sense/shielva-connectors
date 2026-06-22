"""Local dataclass models for the Make connector.

These mirror a small subset of the shared `AuthStatus` / `ConnectorHealth`
contract so callers that import from this module receive a stable surface
even if the shared module's enum values are extended. The dataclasses
expose `@property` shims that resolve to the canonical shared values.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.base_connector import AuthStatus as _SharedAuthStatus
from shared.base_connector import ConnectorHealth as _SharedConnectorHealth


@dataclass
class MakeAuthStatus:
    """Local mirror of AuthStatus — exposes the shared enum via `.value`."""

    state: str = "pending"

    @property
    def value(self) -> _SharedAuthStatus:
        """Return the canonical shared AuthStatus enum value."""
        try:
            return _SharedAuthStatus(self.state)
        except ValueError:
            return _SharedAuthStatus.PENDING


@dataclass
class MakeConnectorHealth:
    """Local mirror of ConnectorHealth — exposes the shared enum via `.value`."""

    state: str = "healthy"

    @property
    def value(self) -> _SharedConnectorHealth:
        """Return the canonical shared ConnectorHealth enum value."""
        try:
            return _SharedConnectorHealth(self.state)
        except ValueError:
            return _SharedConnectorHealth.HEALTHY


@dataclass
class MakeOrganization:
    id: int
    name: str
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MakeTeam:
    id: int
    name: str
    organization_id: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MakeScenario:
    id: int
    name: str
    team_id: Optional[int] = None
    is_active: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MakeExecution:
    id: str
    scenario_id: Optional[int] = None
    status: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MakeHook:
    id: int
    name: str
    type_name: str = "webhook"
    team_id: Optional[int] = None
    url: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ListScenariosRequest:
    team_id: int
    page: int = 1
    pageSize: int = 50


@dataclass
class CreateScenarioRequest:
    team_id: int
    name: str
    blueprint: Dict[str, Any]
    scheduling: Optional[Dict[str, Any]] = None
