"""Result and document dataclasses returned by the Keap connector.

These mirror the canonical ``shared.base_connector`` result types so the gateway
can read either the local fields (``success``, ``healthy``) or the canonical
property shims (``auth_status``, ``health``).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.base_connector import AuthStatus, ConnectorHealth


@dataclass
class InstallResult:
    """Outcome of ``KeapConnector.install()``.

    The gateway inspects ``auth_status``; ``success`` / ``message`` are
    convenience for the connector author and CLI tooling.
    """

    success: bool
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def auth_status(self) -> AuthStatus:
        return AuthStatus.CONNECTED if self.success else AuthStatus.MISSING_CREDENTIALS

    @property
    def health(self) -> ConnectorHealth:
        return ConnectorHealth.HEALTHY if self.success else ConnectorHealth.OFFLINE


@dataclass
class HealthCheckResult:
    """Outcome of ``KeapConnector.health_check()``.

    Exposes both ``health`` and ``auth_status`` shims so the gateway sees the
    same shape as a canonical ``ConnectorStatus``.
    """

    healthy: bool
    message: str = ""
    latency_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def health(self) -> ConnectorHealth:
        return ConnectorHealth.HEALTHY if self.healthy else ConnectorHealth.UNHEALTHY

    @property
    def auth_status(self) -> AuthStatus:
        return AuthStatus.CONNECTED if self.healthy else AuthStatus.FAILED


@dataclass
class ConnectorDocument:
    """Minimal normalized document shape returned by the Keap connector."""

    id: str
    source_id: str
    title: str
    content: str
    content_type: str = "text"
    source_url: Optional[str] = None
    author: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
