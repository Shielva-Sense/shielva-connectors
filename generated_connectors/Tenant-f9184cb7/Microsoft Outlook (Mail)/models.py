"""Local dataclass models for the Outlook Mail connector.

These dataclasses are deliberately decoupled from ``shared.base_connector``;
``@property`` shims expose the canonical ``ConnectorHealth`` / ``AuthStatus``
enum values so call sites that compare against the shared enums keep working
without the connector importing anything beyond the shared base.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from shared.base_connector import AuthStatus, ConnectorHealth


@dataclass
class OutlookMailToken:
    """OAuth2 token envelope as we receive it from Microsoft Identity."""

    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    token_type: str = "Bearer"
    scopes: List[str] = field(default_factory=list)

    @property
    def auth_status(self) -> AuthStatus:
        """Map token presence/expiry to the canonical AuthStatus enum."""
        if not self.access_token:
            return AuthStatus.MISSING_CREDENTIALS
        if self.expires_at and self.expires_at < datetime.utcnow():
            return AuthStatus.TOKEN_EXPIRED
        return AuthStatus.CONNECTED


@dataclass
class OutlookMailHealth:
    """Compact health record used internally by the connector."""

    reachable: bool
    detail: str = ""

    @property
    def health(self) -> ConnectorHealth:
        """Map the boolean to the canonical ConnectorHealth enum."""
        return ConnectorHealth.HEALTHY if self.reachable else ConnectorHealth.DEGRADED


@dataclass
class OutlookMessageStub:
    """Minimal message identity returned by list/search endpoints."""

    id: str
    subject: Optional[str] = None
    from_email: Optional[str] = None
    received_at: Optional[str] = None
