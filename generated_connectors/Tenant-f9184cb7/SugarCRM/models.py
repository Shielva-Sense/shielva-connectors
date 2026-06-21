"""Result and document dataclasses returned by the SugarCRM connector.

These mirror the canonical ``shared.base_connector`` result types so the gateway
can read either the local fields (``success`` / ``healthy``) **or** the canonical
property shims (``auth_status`` / ``health``). The shim resolution is lazy: if
``shared.base_connector`` is importable we use its enums; otherwise we fall back
to plain strings so the module is still usable in isolation (unit tests, CLI).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from shared.base_connector import AuthStatus, ConnectorHealth
except ImportError:  # pragma: no cover — exercised in isolated test runs
    AuthStatus = None  # type: ignore[assignment]
    ConnectorHealth = None  # type: ignore[assignment]


def _auth_connected() -> Any:
    return AuthStatus.CONNECTED if AuthStatus is not None else "connected"


def _auth_missing() -> Any:
    return (
        AuthStatus.MISSING_CREDENTIALS
        if AuthStatus is not None
        else "missing_credentials"
    )


def _auth_failed() -> Any:
    return AuthStatus.FAILED if AuthStatus is not None else "failed"


def _auth_expired() -> Any:
    return AuthStatus.TOKEN_EXPIRED if AuthStatus is not None else "token_expired"


def _health_healthy() -> Any:
    return ConnectorHealth.HEALTHY if ConnectorHealth is not None else "healthy"


def _health_degraded() -> Any:
    return ConnectorHealth.DEGRADED if ConnectorHealth is not None else "degraded"


def _health_offline() -> Any:
    return ConnectorHealth.OFFLINE if ConnectorHealth is not None else "offline"


@dataclass
class InstallResult:
    """Outcome of :meth:`SugarCRMConnector.install`.

    ``success=True`` means the OAuth token exchange (password or auth-code) ran
    cleanly and the connector is ready to call SugarCRM. ``success=False`` plus
    a non-empty ``message`` means the install validation failed — typically
    missing ``site_url`` / ``username`` / ``password`` for the password grant or
    a refused token exchange.
    """

    success: bool
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def auth_status(self) -> Any:
        """Canonical ``AuthStatus`` the gateway reads."""
        return _auth_connected() if self.success else _auth_missing()

    @property
    def health(self) -> Any:
        """Canonical ``ConnectorHealth`` the gateway reads."""
        return _health_healthy() if self.success else _health_offline()


@dataclass
class HealthCheckResult:
    """Outcome of :meth:`SugarCRMConnector.health_check`.

    Exposes both ``health`` and ``auth_status`` shims so the gateway sees the
    same shape as a canonical ``ConnectorStatus``.
    """

    healthy: bool
    message: str = ""
    latency_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def health(self) -> Any:
        return _health_healthy() if self.healthy else _health_degraded()

    @property
    def auth_status(self) -> Any:
        return _auth_connected() if self.healthy else _auth_failed()


@dataclass
class SyncResult:
    """Outcome of an incremental or full sync run."""

    success: bool
    documents_found: int = 0
    documents_synced: int = 0
    documents_failed: int = 0
    message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectorDocument:
    """Normalized document emitted by the connector into the knowledge base."""

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
