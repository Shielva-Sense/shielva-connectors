from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── String enums ──────────────────────────────────────────────────────────────

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


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class InstallResult:
    """Result returned by PowerBIConnector.install()."""

    success: bool
    message: str
    connector_type: str = "powerbi"
    health: ConnectorHealth = ConnectorHealth.OFFLINE
    auth_status: AuthStatus = AuthStatus.FAILED
    connector_id: str = ""


@dataclass
class HealthCheckResult:
    """Result returned by PowerBIConnector.health_check()."""

    healthy: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    health: ConnectorHealth = ConnectorHealth.OFFLINE
    auth_status: AuthStatus = AuthStatus.FAILED


@dataclass
class SyncResult:
    """Result returned by PowerBIConnector.sync()."""

    success: bool
    documents: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: SyncStatus = SyncStatus.COMPLETED
    documents_found: int = 0
    documents_synced: int = 0
    documents_failed: int = 0
    message: str = ""


@dataclass
class ConnectorDocument:
    """Normalized document emitted by the connector into the knowledge base."""

    id: str
    source: str
    type: str
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    synced_at: str = ""
    # Additional fields used internally
    connector_id: str = ""
    tenant_id: str = ""
    source_url: str = ""
