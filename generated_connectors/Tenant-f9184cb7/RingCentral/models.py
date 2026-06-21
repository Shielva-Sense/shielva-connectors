"""
RingCentral connector data models — dataclasses + enums.
No React, no fetch — pure Python data structures only.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SyncStatus(str, enum.Enum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class HealthStatus(str, enum.Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class CallDirection(str, enum.Enum):
    INBOUND = "Inbound"
    OUTBOUND = "Outbound"


class MessageType(str, enum.Enum):
    SMS = "SMS"
    MMS = "MMS"
    PAGER = "Pager"
    FAX = "Fax"
    VOICEMAIL = "VoiceMail"
    TEXT = "Text"


class MeetingStatus(str, enum.Enum):
    NOT_STARTED = "NotStarted"
    IN_PROGRESS = "InProgress"
    FINISHED = "Finished"


class ResourceType(str, enum.Enum):
    CALL_LOG = "call_log"
    MESSAGE = "message"
    EXTENSION = "extension"
    CONTACT = "contact"
    MEETING = "meeting"


# ---------------------------------------------------------------------------
# Core result types
# ---------------------------------------------------------------------------


@dataclass
class InstallResult:
    """Returned by RingCentralConnector.install()."""

    success: bool
    connector_type: str = "ringcentral"
    auth_type: str = "oauth2"
    message: str = ""
    install_fields: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "connector_type": self.connector_type,
            "auth_type": self.auth_type,
            "message": self.message,
            "install_fields": self.install_fields,
        }


@dataclass
class HealthCheckResult:
    """Returned by RingCentralConnector.health_check()."""

    status: HealthStatus
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.status == HealthStatus.HEALTHY

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "healthy": self.healthy,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class ConnectorDocument:
    """Normalized record returned from any list_* method."""

    id: str
    resource_type: ResourceType
    raw: dict[str, Any] = field(default_factory=dict)
    normalized: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "resource_type": self.resource_type.value,
            "raw": self.raw,
            "normalized": self.normalized,
        }


@dataclass
class SyncResult:
    """Returned by RingCentralConnector.sync()."""

    status: SyncStatus
    records_synced: int = 0
    resources: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "records_synced": self.records_synced,
            "resources": self.resources,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# RingCentral-specific value objects
# ---------------------------------------------------------------------------


@dataclass
class OAuthToken:
    """OAuth 2.0 token bundle."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    refresh_token: str = ""
    refresh_token_expires_in: int = 604800
    scope: str = ""
    owner_id: str = ""
    endpoint_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OAuthToken":
        return cls(
            access_token=data.get("access_token", ""),
            token_type=data.get("token_type", "Bearer"),
            expires_in=int(data.get("expires_in", 3600)),
            refresh_token=data.get("refresh_token", ""),
            refresh_token_expires_in=int(data.get("refresh_token_expires_in", 604800)),
            scope=data.get("scope", ""),
            owner_id=data.get("owner_id", ""),
            endpoint_id=data.get("endpoint_id", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "refresh_token_expires_in": self.refresh_token_expires_in,
            "scope": self.scope,
            "owner_id": self.owner_id,
            "endpoint_id": self.endpoint_id,
        }


@dataclass
class PagingInfo:
    """RingCentral pagination envelope."""

    page: int = 1
    per_page: int = 100
    page_start: int = 0
    page_end: int = 0
    total_elements: int = 0
    total_pages: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PagingInfo":
        return cls(
            page=int(data.get("page", 1)),
            per_page=int(data.get("perPage", 100)),
            page_start=int(data.get("pageStart", 0)),
            page_end=int(data.get("pageEnd", 0)),
            total_elements=int(data.get("totalElements", 0)),
            total_pages=int(data.get("totalPages", 1)),
        )


# The gateway consumes these results expecting the canonical ConnectorStatus
# shape from shared.base_connector (`.health: ConnectorHealth`,
# `.auth_status: AuthStatus`). Expose those as derived properties so the
# connector's existing return statements need no changes.
try:
    from shared.base_connector import AuthStatus as _AuthStatus, ConnectorHealth as _ConnectorHealth
except ImportError:
    _AuthStatus = None
    _ConnectorHealth = None


def _install_auth_status(self):
    if _AuthStatus is None:
        return None
    return _AuthStatus.CONNECTED if self.success else _AuthStatus.MISSING_CREDENTIALS


def _install_error(self):
    return getattr(self, "_error_field", None) if self.success else (self.message if hasattr(self, "message") else None)


def _health_health(self):
    if _ConnectorHealth is None:
        return None
    return _ConnectorHealth.HEALTHY if self.healthy else _ConnectorHealth.UNHEALTHY


def _health_auth_status(self):
    if _AuthStatus is None:
        return None
    return _AuthStatus.CONNECTED if self.healthy else _AuthStatus.FAILED


InstallResult.auth_status = property(_install_auth_status)
HealthCheckResult.health = property(_health_health)
HealthCheckResult.auth_status = property(_health_auth_status)
