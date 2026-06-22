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


class MonitorStatus(str, Enum):
    """Datadog monitor alert states."""
    ALERT = "Alert"
    WARN = "Warn"
    NO_DATA = "No Data"
    OK = "OK"
    IGNORED = "Ignored"
    SKIPPED = "Skipped"
    UNKNOWN = "Unknown"


class MonitorType(str, Enum):
    """Datadog monitor types."""
    METRIC_ALERT = "metric alert"
    SERVICE_CHECK = "service check"
    EVENT_ALERT = "event alert"
    QUERY_ALERT = "query alert"
    COMPOSITE = "composite"
    LOG_ALERT = "log alert"


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
