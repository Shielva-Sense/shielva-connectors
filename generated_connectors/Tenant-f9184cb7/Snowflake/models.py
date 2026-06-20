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


class SyncStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    RUNNING = "running"


class StatementStatus(str, Enum):
    """Snowflake async statement execution statuses."""
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTING = "aborting"
    ABORTED = "aborted"
    QUEUED = "queued"
    RESUMING_WAREHOUSE = "resumingWarehouse"
    BLOCKED = "blocked"
    NO_DATA = "noData"


@dataclass
class InstallResult:
    health: ConnectorHealth
    auth_status: AuthStatus
    connector_id: str = ""
    message: str = ""
    account: str = ""


@dataclass
class HealthCheckResult:
    health: ConnectorHealth
    auth_status: AuthStatus
    message: str = ""
    account: str = ""
    username: str = ""


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


@dataclass
class SnowflakeDatabase:
    """Represents a Snowflake database."""
    name: str
    created_on: str = ""
    owner: str = ""
    comment: str = ""
    retention_time: int = 1
    options: str = ""


@dataclass
class SnowflakeSchema:
    """Represents a Snowflake schema within a database."""
    name: str
    database_name: str
    created_on: str = ""
    owner: str = ""
    comment: str = ""
    retention_time: int = 1
    options: str = ""


@dataclass
class SnowflakeTable:
    """Represents a Snowflake table within a database.schema."""
    name: str
    database_name: str
    schema_name: str
    kind: str = "TABLE"
    created_on: str = ""
    owner: str = ""
    comment: str = ""
    rows: int = 0
    bytes_size: int = 0
    cluster_by: str = ""
