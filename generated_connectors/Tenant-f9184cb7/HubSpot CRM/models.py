from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class InstallResult:
    """Result returned by HubSpotConnector.install()."""

    success: bool
    message: str
    connector_id: str = ""


@dataclass
class HealthCheckResult:
    """Result returned by HubSpotConnector.health_check()."""

    healthy: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncResult:
    """Result returned by HubSpotConnector.sync()."""

    success: bool
    records_synced: int = 0
    errors: list[str] = field(default_factory=list)
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
