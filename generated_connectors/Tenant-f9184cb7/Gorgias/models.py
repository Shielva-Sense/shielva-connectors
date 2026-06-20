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


class TicketStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    SPAM = "spam"


class SatisfactionScore(str, Enum):
    GOOD = "good"
    BAD = "bad"
    NEUTRAL = "neutral"


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
class GorgiasTicket:
    """Lightweight typed wrapper for a Gorgias ticket response."""

    id: int
    subject: str
    status: str
    customer_id: int | None
    assignee_user_id: int | None
    tags: list[str]
    created_datetime: str
    updated_datetime: str
    messages_count: int
    channel: str
    is_unread: bool
    spam: bool


@dataclass
class GorgiasCustomer:
    """Lightweight typed wrapper for a Gorgias customer response."""

    id: int
    email: str
    name: str
    external_id: str | None
    created_datetime: str
    updated_datetime: str


@dataclass
class GorgiasTag:
    """Lightweight typed wrapper for a Gorgias tag response."""

    id: int
    name: str
    decoration: str | None


@dataclass
class GorgiasMacro:
    """Lightweight typed wrapper for a Gorgias macro response."""

    id: int
    name: str
    actions: list[dict[str, Any]]
    created_datetime: str
    updated_datetime: str
