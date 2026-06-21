"""Local dataclass shims for the EngageBay connector.

These mirror the canonical `shared.base_connector` enums + dataclasses so the
connector can be referenced in isolation (unit tests, schema tooling) without
pulling the full BaseConnector graph. The `@property` shims preserve the legacy
attribute access pattern (`status.health.value`, `status.auth_status.value`).
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class ConnectorHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNHEALTHY = "unhealthy"


class AuthStatus(str, Enum):
    PENDING = "pending"
    CONNECTED = "connected"
    EXPIRED = "expired"
    FAILED = "failed"
    MISSING_CREDENTIALS = "missing_credentials"
    TOKEN_EXPIRED = "token_expired"
    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"
    INVALID_CREDENTIALS = "invalid_credentials"


@dataclass
class ConnectorStatusLocal:
    """Local mirror of shared.base_connector.ConnectorStatus."""
    connector_id: str
    health: ConnectorHealth
    auth_status: AuthStatus
    connector_type: str = ""
    last_sync: Optional[datetime] = None
    message: str = ""

    @property
    def health_value(self) -> str:
        return self.health.value if isinstance(self.health, ConnectorHealth) else str(self.health)

    @property
    def auth_status_value(self) -> str:
        return self.auth_status.value if isinstance(self.auth_status, AuthStatus) else str(self.auth_status)


@dataclass
class ContactProperty:
    """EngageBay contact property entry — used in create/update payloads."""
    name: str
    value: Any
    field_type: str = "TEXT"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value, "field_type": self.field_type}


@dataclass
class EngageBayContact:
    """Light view over an EngageBay contact response."""
    id: Optional[str] = None
    properties: List[Dict[str, Any]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def email(self) -> Optional[str]:
        for prop in self.properties:
            if prop.get("name") == "email":
                return prop.get("value")
        return None


@dataclass
class EngageBayDeal:
    """Light view over an EngageBay deal response."""
    id: Optional[str] = None
    name: str = ""
    expected_value: float = 0.0
    milestone: str = ""
    contact_ids: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EngageBayTask:
    """Light view over an EngageBay task response."""
    id: Optional[str] = None
    name: str = ""
    due_date: int = 0
    contact_id: Optional[str] = None
    owner_id: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)
