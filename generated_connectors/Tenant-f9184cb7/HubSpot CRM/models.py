from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The gateway consumes these results expecting the canonical ConnectorStatus
# shape from shared.base_connector (`.health: ConnectorHealth`,
# `.auth_status: AuthStatus`). Expose those as derived properties so the
# connector's existing return statements need no changes.
try:
    from shared.base_connector import AuthStatus as _AuthStatus, ConnectorHealth as _ConnectorHealth
except ImportError:  # standalone / test mode
    _AuthStatus = None
    _ConnectorHealth = None


@dataclass
class InstallResult:
    """Result returned by HubSpotConnector.install()."""

    success: bool
    message: str
    connector_id: str = ""

    @property
    def auth_status(self):
        if _AuthStatus is None:
            return None
        return _AuthStatus.CONNECTED if self.success else _AuthStatus.MISSING_CREDENTIALS

    @property
    def error(self):
        return None if self.success else self.message


@dataclass
class HealthCheckResult:
    """Result returned by HubSpotConnector.health_check()."""

    healthy: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def health(self):
        if _ConnectorHealth is None:
            return None
        return _ConnectorHealth.HEALTHY if self.healthy else _ConnectorHealth.UNHEALTHY

    @property
    def auth_status(self):
        if _AuthStatus is None:
            return None
        return _AuthStatus.CONNECTED if self.healthy else _AuthStatus.FAILED

    @property
    def error(self):
        return None if self.healthy else self.message


@dataclass
class SyncResult:
    """Result returned by HubSpotConnector.sync()."""

    success: bool
    records_synced: int = 0
    errors: list[str] = field(default_factory=list)
    message: str = ""


@dataclass
class ConnectorDocument:
    """Normalized document emitted by the connector into the knowledge base."""

    source_id: str
    title: str
    content: str
    connector_id: str
    tenant_id: str
    source_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
