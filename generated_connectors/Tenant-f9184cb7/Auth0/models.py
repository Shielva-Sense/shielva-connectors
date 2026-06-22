"""Auth0 connector data models."""

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


class Auth0UserStatus(str, Enum):
    """Possible Auth0 user states (derived from blocked + email_verified flags)."""

    ACTIVE = "active"
    BLOCKED = "blocked"
    UNVERIFIED = "unverified"


class Auth0ClientType(str, Enum):
    """Auth0 application type values."""

    NATIVE = "native"
    SPA = "spa"
    REGULAR_WEB = "regular_web"
    NON_INTERACTIVE = "non_interactive"


class Auth0ConnectionStrategy(str, Enum):
    """Well-known Auth0 connection strategies."""

    AUTH0 = "auth0"
    GOOGLE_OAUTH2 = "google-oauth2"
    GITHUB = "github"
    SAMLP = "samlp"
    WAAD = "waad"
    ADFS = "adfs"
    AD = "ad"


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
    username: str = ""


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
