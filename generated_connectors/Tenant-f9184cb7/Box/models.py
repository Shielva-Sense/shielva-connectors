"""Dataclasses and enums for the Box connector.

Deliberately self-contained — no imports from shared.* so the module loads
even when the Shielva SDK is absent (standalone testing, gateway AST scan).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class AuthStatus(enum.Enum):
    CONNECTED = "connected"
    MISSING_CREDENTIALS = "missing_credentials"
    INVALID_CREDENTIALS = "invalid_credentials"
    FAILED = "failed"
    PENDING = "pending"
    TOKEN_EXPIRED = "token_expired"


class ConnectorHealth(enum.Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class SyncStatus(enum.Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


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
    user_name: str = ""
    user_login: str = ""


@dataclass
class SyncResult:
    status: SyncStatus
    documents_found: int = 0
    documents_synced: int = 0
    documents_failed: int = 0
    message: str = ""


@dataclass
class ConnectorDocument:
    """Lightweight document container used when BaseConnector is unavailable."""
    id: str
    source: str
    title: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    connector_id: str = ""
    tenant_id: str = ""
