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


class OpportunityStage(str, Enum):
    """Lever canonical pipeline stages (non-exhaustive — stage names are tenant-defined)."""

    LEAD = "lead"
    APPLICANT = "applicant"
    INTERVIEW = "interview"
    OFFER = "offer"
    HIRED = "hired"
    ARCHIVED = "archived"


class PostingState(str, Enum):
    PUBLISHED = "published"
    INTERNAL = "internal"
    CLOSED = "closed"
    DRAFT = "draft"


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
class LeverOpportunity:
    """Typed representation of a Lever opportunity (candidate)."""

    id: str
    name: str
    headline: str
    contact: str
    emails: list[str]
    phones: list[str]
    stage: str
    archived: bool
    created_at: int
    updated_at: int
    tags: list[str]
    links: list[str]
    owner: str
    posting_id: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LeverPosting:
    """Typed representation of a Lever job posting."""

    id: str
    text: str
    state: str
    department: str
    team: str
    location: str
    created_at: int
    updated_at: int
    urls: dict[str, str]
    tags: list[str]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LeverUser:
    """Typed representation of a Lever user (team member)."""

    id: str
    name: str
    email: str
    username: str
    access_role: str
    active: bool
    created_at: int
    raw: dict[str, Any] = field(default_factory=dict)
