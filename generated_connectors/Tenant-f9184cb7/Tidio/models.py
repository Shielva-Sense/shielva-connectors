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


class VisitorStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class ChatbotStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


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
class TidioConversation:
    """Typed representation of a Tidio conversation."""

    id: str
    status: str
    created_at: str = ""
    updated_at: str = ""
    visitor_id: str = ""
    operator_id: str = ""
    unread_count: int = 0


@dataclass
class TidioVisitor:
    """Typed representation of a Tidio visitor."""

    id: str
    email: str = ""
    name: str = ""
    ip: str = ""
    country: str = ""
    city: str = ""
    created_at: str = ""


@dataclass
class TidioChatbot:
    """Typed representation of a Tidio chatbot."""

    id: str
    name: str = ""
    status: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class TidioOperator:
    """Typed representation of a Tidio operator."""

    id: str
    email: str = ""
    name: str = ""
    status: str = ""
