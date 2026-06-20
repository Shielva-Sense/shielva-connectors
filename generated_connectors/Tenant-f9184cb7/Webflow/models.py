"""Webflow connector — standalone dataclass models."""
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


class WebflowResourceType(str, Enum):
    SITE = "webflow_site"
    COLLECTION = "webflow_collection"
    ITEM = "webflow_item"
    PAGE = "webflow_page"


@dataclass
class ConnectorDocument:
    """Normalized document produced by Webflow resource normalizers."""

    id: str
    title: str
    content: str
    type: str = "webflow_resource"
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
class OAuthTokenResponse:
    """Parsed token exchange response from Webflow."""

    access_token: str
    token_type: str = "bearer"
    scope: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)
