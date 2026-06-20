"""Workday connector for Shielva — syncs workers, organizations, job profiles, and locations."""
from __future__ import annotations

from connector import WorkdayConnector
from exceptions import (
    WorkdayAuthError,
    WorkdayError,
    WorkdayNetworkError,
    WorkdayNotFoundError,
    WorkdayRateLimitError,
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
    "WorkdayConnector",
    "WorkdayError",
    "WorkdayAuthError",
    "WorkdayNetworkError",
    "WorkdayNotFoundError",
    "WorkdayRateLimitError",
    "ConnectorHealth",
    "AuthStatus",
    "SyncStatus",
    "InstallResult",
    "HealthCheckResult",
    "SyncResult",
    "ConnectorDocument",
]
