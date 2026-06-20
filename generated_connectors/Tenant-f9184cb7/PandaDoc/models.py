from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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


class DocumentStatus(str, Enum):
    """PandaDoc document statuses."""
    DOCUMENT_DRAFT = "document.draft"
    DOCUMENT_SENT = "document.sent"
    DOCUMENT_VIEWED = "document.viewed"
    DOCUMENT_WAITING_APPROVAL = "document.waiting_approval"
    DOCUMENT_APPROVED = "document.approved"
    DOCUMENT_REJECTED = "document.rejected"
    DOCUMENT_WAITING_PAY = "document.waiting_pay"
    DOCUMENT_PAID = "document.paid"
    DOCUMENT_COMPLETED = "document.completed"
    DOCUMENT_EXPIRED = "document.expired"
    DOCUMENT_DECLINED = "document.declined"
    DOCUMENT_VOIDED = "document.voided"
    DOCUMENT_DELETED = "document.deleted"


class ResourceType(str, Enum):
    DOCUMENT = "document"
    TEMPLATE = "template"
    CONTACT = "contact"
    FORM = "form"
    MEMBER = "member"


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
    workspace_name: str = ""


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
    resource_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
