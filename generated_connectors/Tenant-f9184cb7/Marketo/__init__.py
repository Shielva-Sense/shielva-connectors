"""Shielva connector for Marketo (Adobe marketing automation)."""

from connector import MarketoConnector
from exceptions import (
    MarketoAuthError,
    MarketoError,
    MarketoNetworkError,
    MarketoNotFoundError,
    MarketoRateLimitError,
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
    "MarketoConnector",
    "MarketoError",
    "MarketoAuthError",
    "MarketoNetworkError",
    "MarketoNotFoundError",
    "MarketoRateLimitError",
    "AuthStatus",
    "ConnectorDocument",
    "ConnectorHealth",
    "HealthCheckResult",
    "InstallResult",
    "SyncResult",
    "SyncStatus",
]
