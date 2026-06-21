"""Local dataclasses for Document360 connector with @property shims for
AuthStatus + ConnectorHealth so callers can read symbolic state without
importing the SDK enums directly.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Property shims ────────────────────────────────────────────────────────
# Surface the SDK enums as module-level properties via small wrappers so
# callers can do `models.AuthStatus.CONNECTED` even when the SDK import path
# changes in the future.


class _AuthStatusShim:
    """Lazy passthrough to shared.base_connector.AuthStatus."""

    @property
    def PENDING(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.PENDING

    @property
    def CONNECTED(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.CONNECTED

    @property
    def AUTHENTICATED(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.AUTHENTICATED

    @property
    def TOKEN_EXPIRED(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.TOKEN_EXPIRED

    @property
    def MISSING_CREDENTIALS(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.MISSING_CREDENTIALS

    @property
    def INVALID_CREDENTIALS(self) -> str:
        from shared.base_connector import AuthStatus
        return AuthStatus.INVALID_CREDENTIALS


class _ConnectorHealthShim:
    """Lazy passthrough to shared.base_connector.ConnectorHealth."""

    @property
    def HEALTHY(self) -> str:
        from shared.base_connector import ConnectorHealth
        return ConnectorHealth.HEALTHY

    @property
    def DEGRADED(self) -> str:
        from shared.base_connector import ConnectorHealth
        return ConnectorHealth.DEGRADED

    @property
    def OFFLINE(self) -> str:
        from shared.base_connector import ConnectorHealth
        return ConnectorHealth.OFFLINE

    @property
    def UNHEALTHY(self) -> str:
        from shared.base_connector import ConnectorHealth
        return ConnectorHealth.UNHEALTHY


AuthStatus = _AuthStatusShim()
ConnectorHealth = _ConnectorHealthShim()


# ── Local dataclasses ──────────────────────────────────────────────────────


@dataclass
class Document360Project:
    id: str
    name: str
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def project_id(self) -> str:
        return self.id


@dataclass
class Document360Version:
    id: str
    name: str
    project_id: str = ""
    language_code: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def version_id(self) -> str:
        return self.id


@dataclass
class Document360Category:
    id: str
    title: str
    parent_category_id: Optional[str] = None
    order: Optional[int] = None
    category_type: str = "Folder"
    language_code: str = "en"
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def category_id(self) -> str:
        return self.id


@dataclass
class Document360Article:
    id: str
    title: str
    content: str = ""
    category_id: Optional[str] = None
    language_code: str = "en"
    order: Optional[int] = None
    is_published: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def article_id(self) -> str:
        return self.id

    @property
    def published(self) -> bool:
        return self.is_published


@dataclass
class Document360SearchHit:
    article_id: str
    title: str
    snippet: str = ""
    category_id: Optional[str] = None
    score: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Document360ListResponse:
    items: List[Dict[str, Any]] = field(default_factory=list)
    next_token: Optional[str] = None
    total: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.items)
