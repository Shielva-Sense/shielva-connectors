"""Smartsheet connector — standalone dataclass models."""
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


class ResourceType(str, Enum):
    SHEET = "smartsheet_sheet"
    ROW = "smartsheet_row"
    WORKSPACE = "smartsheet_workspace"
    REPORT = "smartsheet_report"
    FOLDER = "smartsheet_folder"


@dataclass
class ConnectorDocument:
    id: str
    title: str
    content: str
    type: str = "smartsheet_sheet"
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
class SmartsheetUser:
    """Represents the authenticated Smartsheet user from /users/me."""
    id: int
    email: str
    first_name: str = ""
    last_name: str = ""
    admin: bool = False
    licensed_sheet_creator: bool = False

    @property
    def display_name(self) -> str:
        full = f"{self.first_name} {self.last_name}".strip()
        return full or self.email


@dataclass
class SheetColumn:
    """Represents a column definition within a Smartsheet sheet."""
    id: int
    index: int
    title: str
    type: str = ""
    primary: bool = False


@dataclass
class SheetRow:
    """Represents a row within a Smartsheet sheet."""
    id: int
    row_number: int
    sheet_id: int
    cells: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    modified_at: str = ""


@dataclass
class Sheet:
    """Represents a Smartsheet sheet summary."""
    id: int
    name: str
    permalink: str = ""
    access_level: str = ""
    created_at: str = ""
    modified_at: str = ""
    total_row_count: int = 0


@dataclass
class Workspace:
    """Represents a Smartsheet workspace."""
    id: int
    name: str
    access_level: str = ""


@dataclass
class Report:
    """Represents a Smartsheet report."""
    id: int
    name: str
    access_level: str = ""
    created_at: str = ""
    modified_at: str = ""
