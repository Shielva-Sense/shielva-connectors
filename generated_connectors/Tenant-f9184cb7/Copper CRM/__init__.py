"""Copper CRM Shielva connector."""

from .connector import AUTH_TYPE, CONNECTOR_TYPE, CopperConnector
from .exceptions import (
    CopperAuthError,
    CopperError,
    CopperNetworkError,
    CopperNotFoundError,
    CopperRateLimitError,
)
from .models import (
    ConnectorDocument,
    CopperCompany,
    CopperOpportunity,
    CopperPerson,
    CopperTask,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

__all__ = [
    "CONNECTOR_TYPE",
    "AUTH_TYPE",
    "CopperConnector",
    "CopperError",
    "CopperAuthError",
    "CopperNetworkError",
    "CopperNotFoundError",
    "CopperRateLimitError",
    "ConnectorDocument",
    "InstallResult",
    "HealthCheckResult",
    "SyncResult",
    "SyncStatus",
    "CopperPerson",
    "CopperCompany",
    "CopperOpportunity",
    "CopperTask",
]
