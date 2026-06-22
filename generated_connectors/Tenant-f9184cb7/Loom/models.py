"""Loom connector — standalone dataclass models."""
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


class LoomResourceType(str, Enum):
    VIDEO = "video"
    FOLDER = "folder"
    WORKSPACE = "workspace"


class LoomVideoStatus(str, Enum):
    """Possible processing states for a Loom video."""
    PROCESSING = "processing"
    TRANSCODING = "transcoding"
    READY = "ready"
    ERROR = "error"


@dataclass
class ConnectorDocument:
    id: str
    title: str
    content: str
    type: str = "loom_video"
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
class LoomVideo:
    """Represents a Loom video resource."""
    id: str
    title: str
    description: str = ""
    url: str = ""
    status: str = ""
    created_at: str = ""
    updated_at: str = ""
    duration: Optional[int] = None
    folder_id: Optional[str] = None
    workspace_id: Optional[str] = None
    transcript_url: Optional[str] = None


@dataclass
class LoomFolder:
    """Represents a Loom folder resource."""
    id: str
    name: str
    parent_id: Optional[str] = None
    workspace_id: Optional[str] = None
    created_at: str = ""


@dataclass
class LoomWorkspace:
    """Represents a Loom workspace."""
    id: str
    name: str
    created_at: str = ""
    member_count: int = 0
