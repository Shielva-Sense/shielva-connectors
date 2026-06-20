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


class ResourceType(str, Enum):
    PROJECT = "project"
    TODO_LIST = "todolist"
    TODO = "todo"
    MESSAGE = "message"
    DOCUMENT = "document"


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
class BasecampAccount:
    """Represents a Basecamp account (organization) from /authorization.json."""

    id: int
    name: str
    product: str
    href: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BasecampAccount":
        return cls(
            id=int(data.get("id", 0)),
            name=str(data.get("name", "")),
            product=str(data.get("product", "")),
            href=str(data.get("href", "")),
        )
