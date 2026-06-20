"""Miro connector — standalone dataclass models."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AuthStatus(str, Enum):
    CONNECTED = "connected"
    FAILED = "failed"
    MISSING_CREDENTIALS = "missing_credentials"
    INVALID_CREDENTIALS = "invalid_credentials"
    PENDING = "pending"
    TOKEN_EXPIRED = "token_expired"


class ConnectorHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class SyncStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class MiroObjectType(str, Enum):
    BOARD = "board"
    ITEM = "item"
    STICKY_NOTE = "sticky_note"
    CARD = "card"
    SHAPE = "shape"
    TEXT = "text"
    FRAME = "frame"
    IMAGE = "image"


class MiroBoardSharingPolicy(str, Enum):
    PRIVATE = "private"
    VIEW = "view"
    COMMENT = "comment"
    EDIT = "edit"
    TEAM_EDIT = "team_edit"
    TEAM_COMMENT = "team_comment"
    TEAM_VIEW = "team_view"
    COMPANY_EDIT = "company_edit"
    COMPANY_COMMENT = "company_comment"
    COMPANY_VIEW = "company_view"


@dataclass
class ConnectorDocument:
    id: str
    title: str
    content: str
    type: str = "miro_board"
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
    user_name: str = ""
    user_email: str = ""


@dataclass
class SyncResult:
    status: SyncStatus
    documents_found: int = 0
    documents_synced: int = 0
    documents_failed: int = 0
    message: str = ""


@dataclass
class MiroBoard:
    id: str
    name: str
    description: str = ""
    created_at: str = ""
    modified_at: str = ""
    sharing_policy: str = ""
    team_id: str = ""
    owner_id: str = ""
    owner_name: str = ""
    view_link: str = ""


@dataclass
class MiroItem:
    id: str
    type: str
    board_id: str
    content: str = ""
    created_at: str = ""
    modified_at: str = ""
    created_by: str = ""
    modified_by: str = ""
    position_x: Optional[float] = None
    position_y: Optional[float] = None


@dataclass
class MiroTokenInfo:
    user_id: str
    team_id: str
    scopes: List[str] = field(default_factory=list)
    user_name: str = ""
    user_email: str = ""
    team_name: str = ""
