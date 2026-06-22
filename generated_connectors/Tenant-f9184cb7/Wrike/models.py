"""Wrike connector data models — dataclasses and enums only, no React/fetch."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── Status enums ──────────────────────────────────────────────────────────────


class ConnectorHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class AuthStatus(str, Enum):
    CONNECTED = "connected"
    FAILED = "failed"
    MISSING_CREDENTIALS = "missing_credentials"
    INVALID_CREDENTIALS = "invalid_credentials"
    PENDING_OAUTH = "pending_oauth"


class SyncStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    RUNNING = "running"


# ── Wrike resource enums ──────────────────────────────────────────────────────


class WrikeTaskStatus(str, Enum):
    ACTIVE = "Active"
    COMPLETED = "Completed"
    DEFERRED = "Deferred"
    CANCELLED = "Cancelled"


class WrikeImportance(str, Enum):
    HIGH = "High"
    NORMAL = "Normal"
    LOW = "Low"


# ── Result dataclasses ────────────────────────────────────────────────────────


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


# ── Wrike resource models ─────────────────────────────────────────────────────


@dataclass
class WrikeFolder:
    """Represents a Wrike folder or project."""

    id: str
    title: str
    color: str = ""
    created_date: str = ""
    updated_date: str = ""
    description: str = ""
    shared_ids: list[str] = field(default_factory=list)
    child_ids: list[str] = field(default_factory=list)
    scope: str = ""
    project: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> WrikeFolder:
        return cls(
            id=raw.get("id", "") or "",
            title=raw.get("title", "") or "",
            color=raw.get("color", "") or "",
            created_date=raw.get("createdDate", "") or "",
            updated_date=raw.get("updatedDate", "") or "",
            description=raw.get("description", "") or "",
            shared_ids=raw.get("sharedIds", []) or [],
            child_ids=raw.get("childIds", []) or [],
            scope=raw.get("scope", "") or "",
            project=raw.get("project", {}) or {},
        )


@dataclass
class WrikeTask:
    """Represents a Wrike task."""

    id: str
    title: str
    status: str = ""
    importance: str = ""
    created_date: str = ""
    updated_date: str = ""
    due_date: str = ""
    description: str = ""
    assignee_ids: list[str] = field(default_factory=list)
    parent_ids: list[str] = field(default_factory=list)
    custom_status_id: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> WrikeTask:
        dates = raw.get("dates", {}) or {}
        return cls(
            id=raw.get("id", "") or "",
            title=raw.get("title", "") or "",
            status=raw.get("status", "") or "",
            importance=raw.get("importance", "") or "",
            created_date=raw.get("createdDate", "") or "",
            updated_date=raw.get("updatedDate", "") or "",
            due_date=dates.get("due", "") or "",
            description=raw.get("description", "") or "",
            assignee_ids=raw.get("responsibleIds", []) or [],
            parent_ids=raw.get("parentIds", []) or [],
            custom_status_id=raw.get("customStatusId", "") or "",
        )


@dataclass
class WrikeUser:
    """Represents a Wrike user/contact."""

    id: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    role: str = ""
    active: bool = True
    avatar_url: str = ""

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p).strip() or self.email or self.id

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> WrikeUser:
        profiles = raw.get("profiles", []) or []
        email = ""
        role = ""
        if profiles:
            profile = profiles[0] if isinstance(profiles[0], dict) else {}
            email = profile.get("email", "") or ""
            role = profile.get("role", "") or ""
        return cls(
            id=raw.get("id", "") or "",
            first_name=raw.get("firstName", "") or "",
            last_name=raw.get("lastName", "") or "",
            email=email,
            role=role,
            active=bool(raw.get("active", True)),
            avatar_url=raw.get("avatarUrl", "") or "",
        )


@dataclass
class WrikeComment:
    """Represents a Wrike comment."""

    id: str
    author_id: str = ""
    text: str = ""
    created_date: str = ""
    updated_date: str = ""
    task_id: str = ""
    folder_id: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> WrikeComment:
        return cls(
            id=raw.get("id", "") or "",
            author_id=raw.get("authorId", "") or "",
            text=raw.get("text", "") or "",
            created_date=raw.get("createdDate", "") or "",
            updated_date=raw.get("updatedDate", "") or "",
            task_id=raw.get("taskId", "") or "",
            folder_id=raw.get("folderId", "") or "",
        )
