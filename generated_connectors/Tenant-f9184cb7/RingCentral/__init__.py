"""
RingCentral connector for Shielva.

Usage::

    from ringcentral_connector import RingCentralConnector, CONNECTOR_TYPE, AUTH_TYPE

    connector = RingCentralConnector(
        tenant_id="Tenant-xxxx",
        connector_id="my-rc-connector",
        config={
            "client_id": "...",
            "client_secret": "...",
            "access_token": "...",
            "refresh_token": "...",
        },
    )
"""

from connector import AUTH_TYPE, CONNECTOR_TYPE, RingCentralConnector
from exceptions import (
    RingCentralAuthError,
    RingCentralError,
    RingCentralNetworkError,
    RingCentralNotFoundError,
    RingCentralRateLimitError,
)
from models import (
    ConnectorDocument,
    HealthCheckResult,
    HealthStatus,
    InstallResult,
    OAuthToken,
    PagingInfo,
    SyncResult,
    SyncStatus,
)

__all__ = [
    "CONNECTOR_TYPE",
    "AUTH_TYPE",
    "RingCentralConnector",
    "RingCentralError",
    "RingCentralAuthError",
    "RingCentralNetworkError",
    "RingCentralNotFoundError",
    "RingCentralRateLimitError",
    "ConnectorDocument",
    "HealthCheckResult",
    "HealthStatus",
    "InstallResult",
    "OAuthToken",
    "PagingInfo",
    "SyncResult",
    "SyncStatus",
]
