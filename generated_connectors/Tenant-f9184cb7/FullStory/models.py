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


class FullStoryResourceType(str, Enum):
    SESSION = "session_recording"
    USER = "user"
    SEGMENT = "segment"
    EVENT = "event"


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
class FullStorySession:
    """Normalized FullStory session recording record."""

    session_id: str
    uid: str = ""
    created_time: str = ""
    duration_ms: int = 0
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "uid": self.uid,
            "created_time": self.created_time,
            "duration_ms": self.duration_ms,
            "properties": self.properties,
        }


@dataclass
class FullStoryUser:
    """Normalized FullStory user record."""

    uid: str
    display_name: str = ""
    email: str = ""
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "display_name": self.display_name,
            "email": self.email,
            "properties": self.properties,
        }


@dataclass
class FullStorySegment:
    """Normalized FullStory segment record."""

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
