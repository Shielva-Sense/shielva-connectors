"""Local dataclass models for the Clockify connector.

These mirror the canonical shared.base_connector enums via @property shims so
callers can treat ConnectorStatusInfo / AuthStatusInfo interchangeably with the
SDK types while keeping the connector self-contained.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class WorkspaceModel:
    id: str
    name: str
    hourly_rate: Optional[Dict[str, Any]] = None
    memberships: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def workspace_id(self) -> str:
        """Shim — canonical accessor for the workspace identifier."""
        return self.id


@dataclass
class ProjectModel:
    id: str
    name: str
    workspace_id: str
    client_id: Optional[str] = None
    archived: bool = False
    billable: bool = False
    color: Optional[str] = None
    hourly_rate: Optional[Dict[str, Any]] = None

    @property
    def project_id(self) -> str:
        return self.id


@dataclass
class ClientModel:
    id: str
    name: str
    workspace_id: str
    archived: bool = False
    address: Optional[str] = None
    email: Optional[str] = None

    @property
    def client_id(self) -> str:
        return self.id


@dataclass
class TimeEntryModel:
    id: str
    workspace_id: str
    user_id: str
    description: str = ""
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    tag_ids: List[str] = field(default_factory=list)
    billable: bool = False
    start: Optional[str] = None
    end: Optional[str] = None
    duration: Optional[str] = None

    @property
    def entry_id(self) -> str:
        return self.id


@dataclass
class AuthStatusInfo:
    """Local shim around shared.base_connector.AuthStatus.

    Holds the canonical AuthStatus value plus a human-readable message; the
    @property shims let callers read the same surface as the SDK enum without
    importing it.
    """
    status: str
    message: str = ""

    @property
    def auth_status(self) -> str:  # SDK-style accessor
        return self.status

    @property
    def is_connected(self) -> bool:
        return self.status == "connected"


@dataclass
class ConnectorHealthInfo:
    """Local shim around shared.base_connector.ConnectorHealth."""
    health: str
    message: str = ""

    @property
    def connector_health(self) -> str:
        return self.health

    @property
    def is_healthy(self) -> bool:
        return self.health == "healthy"
