"""Figma connector — standalone dataclass models."""
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


class FigmaObjectType(str, Enum):
    FILE = "file"
    PROJECT = "project"
    COMPONENT = "component"
    COMMENT = "comment"
    TEAM = "team"


class FigmaNodeType(str, Enum):
    DOCUMENT = "DOCUMENT"
    CANVAS = "CANVAS"
    FRAME = "FRAME"
    GROUP = "GROUP"
    VECTOR = "VECTOR"
    TEXT = "TEXT"
    COMPONENT = "COMPONENT"
    COMPONENT_SET = "COMPONENT_SET"
    INSTANCE = "INSTANCE"


@dataclass
class ConnectorDocument:
    id: str
    title: str
    content: str
    type: str = "figma_file"
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
class FigmaUser:
    id: str
    handle: str
    email: str = ""
    img_url: str = ""


@dataclass
class FigmaFile:
    key: str
    name: str
    last_modified: str = ""
    thumbnail_url: str = ""
    version: str = ""


@dataclass
class FigmaProject:
    id: str
    name: str
    team_id: str = ""


@dataclass
class FigmaComponent:
    key: str
    name: str
    file_key: str = ""
    node_id: str = ""
    description: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class FigmaComment:
    id: str
    message: str
    file_key: str
    created_at: str = ""
    resolved_at: Optional[str] = None
    user_handle: str = ""
