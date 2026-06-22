"""Local dataclass models for the Grafana connector.

These are simple, dependency-free response shims (no Pydantic) used to give
callers a stable typed surface without coupling to shared.base_connector
internals. The `@property` shims expose the most-used fields of
ConnectorHealth + AuthStatus so consumers can pattern-match on a plain dict
or a typed object interchangeably.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GrafanaOrg:
    """Grafana organization (GET /api/org)."""

    id: int = 0
    name: str = ""
    address: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        """Shim: an org with an id is considered healthy (parity with ConnectorHealth.HEALTHY)."""
        return self.id > 0

    @property
    def is_authenticated(self) -> bool:
        """Shim: presence of an org implies the token authenticated (AuthStatus.CONNECTED)."""
        return self.id > 0


@dataclass
class GrafanaDashboard:
    """Grafana dashboard search hit (GET /api/search)."""

    uid: str = ""
    id: int = 0
    title: str = ""
    type: str = "dash-db"
    tags: List[str] = field(default_factory=list)
    folder_uid: Optional[str] = None
    folder_title: Optional[str] = None
    url: Optional[str] = None

    @property
    def is_healthy(self) -> bool:
        return bool(self.uid)


@dataclass
class GrafanaFolder:
    """Grafana folder (GET /api/folders)."""

    uid: str = ""
    id: int = 0
    title: str = ""
    url: Optional[str] = None


@dataclass
class GrafanaDataSource:
    """Grafana datasource (GET /api/datasources)."""

    id: int = 0
    uid: str = ""
    name: str = ""
    type: str = ""
    url: str = ""
    access: str = "proxy"
    is_default: bool = False


@dataclass
class GrafanaAlertRule:
    """Grafana alert rule (GET /api/v1/provisioning/alert-rules)."""

    uid: str = ""
    title: str = ""
    condition: str = ""
    folder_uid: Optional[str] = None
    no_data_state: str = "NoData"
    exec_err_state: str = "Alerting"


@dataclass
class GrafanaUser:
    """Grafana user (GET /api/users)."""

    id: int = 0
    name: str = ""
    login: str = ""
    email: str = ""
    is_admin: bool = False


@dataclass
class GrafanaTeam:
    """Grafana team (GET /api/teams/search)."""

    id: int = 0
    uid: str = ""
    name: str = ""
    email: str = ""
    member_count: int = 0
