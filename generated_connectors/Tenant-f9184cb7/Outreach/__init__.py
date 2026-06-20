"""Shielva connector for Outreach sales engagement platform."""

from connector import OutreachConnector
from exceptions import (
    OutreachAuthError,
    OutreachError,
    OutreachNetworkError,
    OutreachNotFoundError,
    OutreachRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

__all__ = [
    "OutreachConnector",
    "OutreachError",
    "OutreachAuthError",
    "OutreachNetworkError",
    "OutreachNotFoundError",
    "OutreachRateLimitError",
    "ConnectorHealth",
    "AuthStatus",
    "SyncStatus",
    "InstallResult",
    "HealthCheckResult",
    "SyncResult",
    "ConnectorDocument",
]
