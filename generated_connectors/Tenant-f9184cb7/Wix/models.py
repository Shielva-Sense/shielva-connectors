"""Local dataclasses for the Wix connector — with @property shims for AuthStatus / ConnectorHealth."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class WixSite:
    """Lightweight Wix site model used by helpers/normalizer."""
    id: str
    display_name: str = ""
    url: str = ""
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WixProduct:
    """Lightweight Wix Stores product model."""
    id: str
    name: str = ""
    sku: str = ""
    price: Optional[float] = None
    currency: str = ""
    stock: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WixOrder:
    """Lightweight Wix Ecom order model."""
    id: str
    number: str = ""
    total: Optional[float] = None
    currency: str = ""
    status: str = ""
    buyer_email: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WixContact:
    """Lightweight Wix Contact model."""
    id: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── @property shims so callers can do model.auth_status / model.health
# ── without importing AuthStatus / ConnectorHealth directly. The shims
# ── return enum values lazily so unit tests that don't have the SDK
# ── installed still pass (they import models without crashing).

class _AuthStatusShim:
    @property
    def CONNECTED(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.CONNECTED.value

    @property
    def PENDING(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.PENDING.value

    @property
    def MISSING_CREDENTIALS(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.MISSING_CREDENTIALS.value

    @property
    def TOKEN_EXPIRED(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.TOKEN_EXPIRED.value

    @property
    def AUTHENTICATED(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.AUTHENTICATED.value


class _ConnectorHealthShim:
    @property
    def HEALTHY(self) -> str:
        from shared.base_connector import ConnectorHealth
        return ConnectorHealth.HEALTHY.value

    @property
    def DEGRADED(self) -> str:
        from shared.base_connector import ConnectorHealth
        return ConnectorHealth.DEGRADED.value

    @property
    def OFFLINE(self) -> str:
        from shared.base_connector import ConnectorHealth
        return ConnectorHealth.OFFLINE.value


AuthStatusShim = _AuthStatusShim()
ConnectorHealthShim = _ConnectorHealthShim()
