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


class HeapResourceType(str, Enum):
    USER = "user"
    EVENT = "event"
    SEGMENT = "segment"
    FUNNEL = "funnel"
    USER_PROPERTY = "user_property"


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


@dataclass
class HeapUser:
    """Normalized Heap user record."""

    identity: str
    properties: dict[str, Any] = field(default_factory=dict)
    account_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "properties": self.properties,
            "account_id": self.account_id,
        }


@dataclass
class HeapEvent:
    """Normalized Heap event record."""

    event_name: str
    identity: str = ""
    timestamp: str = ""
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_name": self.event_name,
            "identity": self.identity,
            "timestamp": self.timestamp,
            "properties": self.properties,
        }


@dataclass
class HeapSegment:
    """Normalized Heap segment record."""

    segment_id: str
    name: str
    description: str = ""
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "name": self.name,
            "description": self.description,
            "count": self.count,
        }
