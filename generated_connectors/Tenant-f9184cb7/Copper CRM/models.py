"""Copper CRM connector data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SyncStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ResourceType(str, Enum):
    PERSON = "person"
    COMPANY = "company"
    OPPORTUNITY = "opportunity"
    TASK = "task"


class OpportunityStatus(str, Enum):
    OPEN = "Open"
    WON = "Won"
    LOST = "Lost"
    ABANDONED = "Abandoned"


class TaskStatus(str, Enum):
    OPEN = "Open"
    COMPLETED = "Completed"


# ---------------------------------------------------------------------------
# Generic connector envelope types
# ---------------------------------------------------------------------------


@dataclass
class ConnectorDocument:
    """Normalised document surfaced by sync and list methods."""

    id: str
    resource_type: str
    raw: dict[str, Any]
    display_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "resource_type": self.resource_type,
            "display_name": self.display_name,
            "metadata": self.metadata,
            "raw": self.raw,
        }


@dataclass
class InstallResult:
    """Returned by CopperConnector.install()."""

    success: bool
    connector_id: str = ""
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "connector_id": self.connector_id,
            "error": self.error,
            "details": self.details,
        }


@dataclass
class HealthCheckResult:
    """Returned by CopperConnector.health_check()."""

    healthy: bool
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class SyncResult:
    """Returned by CopperConnector.sync()."""

    status: SyncStatus
    total_synced: int = 0
    resource_counts: dict[str, int] = field(default_factory=dict)
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "total_synced": self.total_synced,
            "resource_counts": self.resource_counts,
            "error": self.error,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Copper-specific typed models (thin wrappers for documentation / typing)
# ---------------------------------------------------------------------------


@dataclass
class CopperPerson:
    """Thin typed wrapper around a Copper People API record."""

    id: int
    name: str
    emails: list[dict[str, str]] = field(default_factory=list)
    phone_numbers: list[dict[str, str]] = field(default_factory=list)
    company_id: int | None = None
    company_name: str | None = None
    title: str | None = None
    details: str | None = None
    date_created: int | None = None
    date_modified: int | None = None
    tags: list[str] = field(default_factory=list)
    custom_fields: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "CopperPerson":
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("name") or "",
            emails=raw.get("emails") or [],
            phone_numbers=raw.get("phone_numbers") or [],
            company_id=raw.get("company_id"),
            company_name=raw.get("company_name"),
            title=raw.get("title"),
            details=raw.get("details"),
            date_created=raw.get("date_created"),
            date_modified=raw.get("date_modified"),
            tags=raw.get("tags") or [],
            custom_fields=raw.get("custom_fields") or [],
            raw=raw,
        )


@dataclass
class CopperCompany:
    """Thin typed wrapper around a Copper Companies API record."""

    id: int
    name: str
    email_domain: str | None = None
    phone_numbers: list[dict[str, str]] = field(default_factory=list)
    details: str | None = None
    date_created: int | None = None
    date_modified: int | None = None
    tags: list[str] = field(default_factory=list)
    custom_fields: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "CopperCompany":
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("name") or "",
            email_domain=raw.get("email_domain"),
            phone_numbers=raw.get("phone_numbers") or [],
            details=raw.get("details"),
            date_created=raw.get("date_created"),
            date_modified=raw.get("date_modified"),
            tags=raw.get("tags") or [],
            custom_fields=raw.get("custom_fields") or [],
            raw=raw,
        )


@dataclass
class CopperOpportunity:
    """Thin typed wrapper around a Copper Opportunities API record."""

    id: int
    name: str
    status: str = ""
    monetary_value: float | None = None
    company_id: int | None = None
    company_name: str | None = None
    assignee_id: int | None = None
    close_date: str | None = None
    details: str | None = None
    date_created: int | None = None
    date_modified: int | None = None
    tags: list[str] = field(default_factory=list)
    custom_fields: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "CopperOpportunity":
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("name") or "",
            status=raw.get("status") or "",
            monetary_value=raw.get("monetary_value"),
            company_id=raw.get("company_id"),
            company_name=raw.get("company_name"),
            assignee_id=raw.get("assignee_id"),
            close_date=raw.get("close_date"),
            details=raw.get("details"),
            date_created=raw.get("date_created"),
            date_modified=raw.get("date_modified"),
            tags=raw.get("tags") or [],
            custom_fields=raw.get("custom_fields") or [],
            raw=raw,
        )


@dataclass
class CopperTask:
    """Thin typed wrapper around a Copper Tasks API record."""

    id: int
    name: str
    status: str = ""
    due_date: int | None = None
    reminder_date: int | None = None
    assignee_id: int | None = None
    details: str | None = None
    date_created: int | None = None
    date_modified: int | None = None
    tags: list[str] = field(default_factory=list)
    custom_fields: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "CopperTask":
        return cls(
            id=int(raw.get("id", 0)),
            name=raw.get("name") or "",
            status=raw.get("status") or "",
            due_date=raw.get("due_date"),
            reminder_date=raw.get("reminder_date"),
            assignee_id=raw.get("assignee_id"),
            details=raw.get("details"),
            date_created=raw.get("date_created"),
            date_modified=raw.get("date_modified"),
            tags=raw.get("tags") or [],
            custom_fields=raw.get("custom_fields") or [],
            raw=raw,
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
