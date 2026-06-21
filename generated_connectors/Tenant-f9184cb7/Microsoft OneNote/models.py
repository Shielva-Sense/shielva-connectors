"""Local dataclass models for the OneNote connector.

These dataclasses are deliberately decoupled from ``shared.base_connector``;
``@property`` shims expose the canonical ``ConnectorHealth`` / ``AuthStatus``
enum values so call sites that compare against the shared enums keep working
without the connector importing anything beyond the shared base.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.base_connector import AuthStatus, ConnectorHealth


@dataclass
class OneNoteToken:
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
class OneNoteHealth:
    """Compact health record used internally by the connector."""

    reachable: bool
    detail: str = ""

    @property
    def health(self) -> ConnectorHealth:
        """Map the boolean to the canonical ConnectorHealth enum."""
        return ConnectorHealth.HEALTHY if self.reachable else ConnectorHealth.DEGRADED


@dataclass
class Notebook:
    """OneNote notebook resource."""

    id: str
    display_name: str = ""
    is_default: bool = False
    user_role: str = ""
    is_shared: bool = False
    created_date_time: Optional[str] = None
    last_modified_date_time: Optional[str] = None

    @property
    def name(self) -> str:
        return self.display_name


@dataclass
class Section:
    """OneNote section resource."""

    id: str
    display_name: str = ""
    is_default: bool = False
    created_date_time: Optional[str] = None
    last_modified_date_time: Optional[str] = None
    parent_notebook_id: str = ""
    parent_section_group_id: str = ""

    @property
    def name(self) -> str:
        return self.display_name


@dataclass
class SectionGroup:
    """OneNote section group resource."""

    id: str
    display_name: str = ""
    created_date_time: Optional[str] = None
    last_modified_date_time: Optional[str] = None
    parent_notebook_id: str = ""

    @property
    def name(self) -> str:
        return self.display_name


@dataclass
class Page:
    """OneNote page resource."""

    id: str
    title: str = ""
    content_url: str = ""
    created_date_time: Optional[str] = None
    last_modified_date_time: Optional[str] = None
    parent_section_id: str = ""
    parent_notebook_id: str = ""
    level: int = 0
    order: int = 0
    links: Dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.title
