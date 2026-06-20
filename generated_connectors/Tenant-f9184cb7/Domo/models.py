"""Domo connector — standalone dataclass models and enums."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AuthStatus(str, Enum):
    CONNECTED = "connected"
    FAILED = "failed"
    MISSING_CREDENTIALS = "missing_credentials"
    INVALID_CREDENTIALS = "invalid_credentials"


class ConnectorHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class SyncStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class DomoResourceType(str, Enum):
    DATASET = "dataset"
    PAGE = "page"
    USER = "user"
    GROUP = "group"


@dataclass
class ConnectorDocument:
    id: str
    title: str
    content: str
    type: str = "domo_resource"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InstallResult:
    health: ConnectorHealth
    auth_status: AuthStatus
    connector_id: str
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
class TokenResponse:
    access_token: str
    expires_in: int = 3600
    token_type: str = "bearer"
    error: Optional[str] = None


@dataclass
class DomoDataset:
    id: str
    name: str
    description: str = ""
    row_count: int = 0
    column_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    owner: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DomoPage:
    id: int
    name: str
    parent_id: Optional[int] = None
    card_count: int = 0
    visibility: str = ""


@dataclass
class DomoUser:
    id: int
    name: str
    email: str = ""
    role: str = ""
    title: str = ""
    department: str = ""


@dataclass
class DomoGroup:
    id: int
    name: str
    member_count: int = 0
    default: bool = False
    active: bool = True
