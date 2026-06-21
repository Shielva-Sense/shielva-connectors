"""Local request dataclasses for the OneLogin connector.

Lightweight typed shapes used by the public connector API. The shared enums
(`AuthStatus`, `ConnectorHealth`) are re-exported for caller convenience.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.base_connector import AuthStatus, ConnectorHealth


@dataclass
class CreateUserRequest:
    email: str
    firstname: str
    lastname: str
    username: Optional[str] = None
    password: Optional[str] = None
    role_ids: List[int] = field(default_factory=list)


@dataclass
class UpdateUserRequest:
    user_id: int
    fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ListUsersRequest:
    limit: int = 50
    after_cursor: Optional[str] = None
    email: Optional[str] = None


@dataclass
class SearchUsersRequest:
    query: str
    limit: int = 50


@dataclass
class ListEventsRequest:
    limit: int = 50
    since: Optional[str] = None
    event_type_id: Optional[int] = None


@dataclass
class OneLoginConnectorIdentity:
    """Lightweight identity object exposing the shared status enums."""

    connector_type: str = "onelogin"
    auth_type: str = "oauth2_client_credentials"

    @property
    def AuthStatus(self):  # noqa: N802
        return AuthStatus

    @property
    def ConnectorHealth(self):  # noqa: N802
        return ConnectorHealth


__all__ = [
    "AuthStatus",
    "ConnectorHealth",
    "CreateUserRequest",
    "ListEventsRequest",
    "ListUsersRequest",
    "OneLoginConnectorIdentity",
    "SearchUsersRequest",
    "UpdateUserRequest",
]
