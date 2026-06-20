from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConnectorHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class AuthStatus(str, Enum):
    CONNECTED = "connected"
    FAILED = "failed"
    MISSING_CREDENTIALS = "missing_credentials"
    INVALID_CREDENTIALS = "invalid_credentials"


class SyncStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    RUNNING = "running"


class AlertPolicyIncidentPreference(str, Enum):
    """New Relic alert policy incident preference."""
    PER_POLICY = "PER_POLICY"
    PER_CONDITION = "PER_CONDITION"
    PER_CONDITION_AND_TARGET = "PER_CONDITION_AND_TARGET"


class IncidentStatus(str, Enum):
    """New Relic incident status values."""
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    CLOSED = "closed"


class ApplicationHealthStatus(str, Enum):
    """New Relic APM application health status."""
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    GRAY = "gray"
    UNKNOWN = "unknown"


class NewRelicRegion(str, Enum):
    """New Relic data center regions."""
    US = "US"
    EU = "EU"


@dataclass
class InstallResult:
    health: ConnectorHealth
    auth_status: AuthStatus
    connector_id: str = ""
    message: str = ""


@dataclass
class HealthCheckResult:
    health: ConnectorHealth
    auth_status: AuthStatus
    message: str = ""


@dataclass
class SyncResult:
    status: SyncStatus
    documents_found: int = 0
    documents_synced: int = 0
    documents_failed: int = 0
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
