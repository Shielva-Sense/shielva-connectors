"""Local dataclasses + @property shims for the Lightspeed Retail connector.

These mirror the AuthStatus / ConnectorHealth string-enum shapes from
shared.base_connector and expose @property accessors used by the
connector's tests so they remain decoupled from the SDK's exact enum
class identity.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class LightspeedAuthStatus:
    """Local shim of an OAuth auth state for the Lightspeed connector."""

    status: str = "pending"
    message: str = ""

    @property
    def is_connected(self) -> bool:
        return self.status == "connected"

    @property
    def is_expired(self) -> bool:
        return self.status in ("expired", "token_expired")

    @property
    def is_missing(self) -> bool:
        return self.status == "missing_credentials"


@dataclass
class LightspeedConnectorHealth:
    """Local shim of connector health for the Lightspeed connector."""

    health: str = "healthy"
    message: str = ""

    @property
    def is_healthy(self) -> bool:
        return self.health == "healthy"

    @property
    def is_degraded(self) -> bool:
        return self.health == "degraded"

    @property
    def is_offline(self) -> bool:
        return self.health == "offline"


@dataclass
class LightspeedItem:
    """Subset of a Lightspeed Retail Item resource used by the connector."""

    item_id: int
    description: str
    default_price: float = 0.0
    default_cost: float = 0.0
    category_id: Optional[int] = None
    item_type: str = "default"
    custom_sku: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def margin(self) -> float:
        """Convenience: simple per-unit margin (price - cost)."""
        try:
            return float(self.default_price) - float(self.default_cost)
        except (TypeError, ValueError):
            return 0.0


@dataclass
class LightspeedCustomer:
    """Subset of a Lightspeed Retail Customer resource used by the connector."""

    customer_id: int
    first_name: str = ""
    last_name: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts).strip()


@dataclass
class LightspeedSale:
    """Subset of a Lightspeed Retail Sale resource used by the connector."""

    sale_id: int
    customer_id: Optional[int] = None
    completed: bool = False
    total: float = 0.0
    created_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_completed(self) -> bool:
        return bool(self.completed)


@dataclass
class ListResponse:
    """Generic Lightspeed list-response envelope.

    Lightspeed wraps lists in a top-level key (e.g. "Item", "Customer") plus
    an "@attributes" key with pagination metadata. This dataclass exposes
    the relevant pieces for the connector + tests.
    """

    items: List[Dict[str, Any]] = field(default_factory=list)
    count: int = 0
    offset: int = 0
    limit: int = 0
    next_offset: Optional[int] = None

    @property
    def has_more(self) -> bool:
        if self.next_offset is None:
            return False
        return self.next_offset < self.count
