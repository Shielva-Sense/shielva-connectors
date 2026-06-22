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
    PENDING = "pending"
    FAILED = "failed"
    MISSING_CREDENTIALS = "missing_credentials"
    INVALID_CREDENTIALS = "invalid_credentials"
    EXPIRED = "expired"


class SyncStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    RUNNING = "running"


class InvoiceStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    DELETED = "DELETED"
    AUTHORISED = "AUTHORISED"
    PAID = "PAID"
    VOIDED = "VOIDED"


class ContactStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class AccountType(str, Enum):
    BANK = "BANK"
    CURRENT = "CURRENT"
    CURRLIAB = "CURRLIAB"
    DEPRECIATN = "DEPRECIATN"
    DIRECTCOSTS = "DIRECTCOSTS"
    EQUITY = "EQUITY"
    EXPENSE = "EXPENSE"
    FIXED = "FIXED"
    INVENTORY = "INVENTORY"
    LIABILITY = "LIABILITY"
    NONCURRENT = "NONCURRENT"
    OTHERINCOME = "OTHERINCOME"
    OVERHEADS = "OVERHEADS"
    PREPAYMENT = "PREPAYMENT"
    REVENUE = "REVENUE"
    SALES = "SALES"
    TERMLIAB = "TERMLIAB"
    PAYGLIABILITY = "PAYGLIABILITY"
    SUPERANNUATIONEXPENSE = "SUPERANNUATIONEXPENSE"
    SUPERANNUATIONLIABILITY = "SUPERANNUATIONLIABILITY"
    WAGESEXPENSE = "WAGESEXPENSE"


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
    organisation_name: str = ""


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
