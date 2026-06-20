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


class FlagKind(str, Enum):
    """LaunchDarkly feature flag kinds."""
    BOOLEAN = "boolean"
    MULTIVARIATE = "multivariate"


class MemberRole(str, Enum):
    """LaunchDarkly built-in member roles."""
    OWNER = "owner"
    ADMIN = "admin"
    WRITER = "writer"
    READER = "reader"
    NO_ACCESS = "no_access"


class AuditAction(str, Enum):
    """Common LaunchDarkly audit log action categories."""
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    OTHER = "other"


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
