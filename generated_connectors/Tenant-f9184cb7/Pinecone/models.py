"""Local dataclasses + AuthStatus / ConnectorHealth property shims for the Pinecone connector.

These mirror a subset of shared.base_connector enums via @property shims so callers
inside this package can `from models import AuthStatus, ConnectorHealth` without
re-importing the SDK. The real enums still come from `shared.base_connector` — the
shims forward to them, they never re-define values.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.base_connector import AuthStatus as _SDKAuthStatus
from shared.base_connector import ConnectorHealth as _SDKConnectorHealth


class _AuthStatusShim:
    """@property shim so `models.AuthStatus.CONNECTED` resolves to the SDK enum."""

    @property
    def PENDING(self) -> _SDKAuthStatus:
        return _SDKAuthStatus.PENDING

    @property
    def CONNECTED(self) -> _SDKAuthStatus:
        return _SDKAuthStatus.CONNECTED

    @property
    def EXPIRED(self) -> _SDKAuthStatus:
        return _SDKAuthStatus.EXPIRED

    @property
    def FAILED(self) -> _SDKAuthStatus:
        return _SDKAuthStatus.FAILED

    @property
    def MISSING_CREDENTIALS(self) -> _SDKAuthStatus:
        return _SDKAuthStatus.MISSING_CREDENTIALS

    @property
    def TOKEN_EXPIRED(self) -> _SDKAuthStatus:
        return _SDKAuthStatus.TOKEN_EXPIRED

    @property
    def AUTHENTICATED(self) -> _SDKAuthStatus:
        return _SDKAuthStatus.AUTHENTICATED

    @property
    def INVALID_CREDENTIALS(self) -> _SDKAuthStatus:
        return _SDKAuthStatus.INVALID_CREDENTIALS


class _ConnectorHealthShim:
    """@property shim so `models.ConnectorHealth.HEALTHY` resolves to the SDK enum."""

    @property
    def HEALTHY(self) -> _SDKConnectorHealth:
        return _SDKConnectorHealth.HEALTHY

    @property
    def DEGRADED(self) -> _SDKConnectorHealth:
        return _SDKConnectorHealth.DEGRADED

    @property
    def OFFLINE(self) -> _SDKConnectorHealth:
        return _SDKConnectorHealth.OFFLINE

    @property
    def UNHEALTHY(self) -> _SDKConnectorHealth:
        return _SDKConnectorHealth.UNHEALTHY


AuthStatus = _AuthStatusShim()
ConnectorHealth = _ConnectorHealthShim()


# ── Local dataclasses for typed request/response shape ────────────────────────


@dataclass
class IndexSpec:
    """Pinecone index spec — control-plane describe_index response shape."""

    name: str
    dimension: int
    metric: str = "cosine"
    host: str = ""
    status: Dict[str, Any] = field(default_factory=dict)
    spec: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorRecord:
    """A single vector record for upsert/update."""

    id: str
    values: List[float]
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class QueryMatch:
    """A single match returned from /query."""

    id: str
    score: float
    values: List[float] = field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class QueryResponse:
    """Pinecone /query response."""

    matches: List[QueryMatch] = field(default_factory=list)
    namespace: str = ""
