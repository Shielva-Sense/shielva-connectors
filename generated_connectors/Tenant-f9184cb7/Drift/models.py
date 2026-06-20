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


class ConversationStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    PENDING = "pending"


class ContactType(str, Enum):
    KNOWN = "known"
    ANONYMOUS = "anonymous"


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
class DriftConversation:
    """Typed representation of a Drift conversation."""

    id: int
    status: str
    created_at: int
    updated_at: int
    subject: str = ""
    contact_id: int = 0
    agent_id: int = 0


@dataclass
class DriftContact:
    """Typed representation of a Drift contact."""

    id: int
    email: str = ""
    name: str = ""
    phone: str = ""
    created_at: int = 0
    updated_at: int = 0


@dataclass
class DriftAccount:
    """Typed representation of a Drift account."""

    id: int
    name: str = ""
    domain: str = ""
    created_at: int = 0


@dataclass
class DriftMessage:
    """Typed representation of a Drift message."""

    id: int
    conversation_id: int
    body: str = ""
    author_id: int = 0
    author_type: str = ""
    created_at: int = 0
