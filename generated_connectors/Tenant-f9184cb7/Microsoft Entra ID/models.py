"""Pydantic schemas + lightweight dataclass projections for Microsoft Entra ID.

The connector boundary returns raw Microsoft Graph dicts (camelCase) to callers;
these schemas exist as light projections for tooling that wants typed access.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from pydantic import BaseModel, ConfigDict, Field

    class _GraphModel(BaseModel):
        model_config = ConfigDict(populate_by_name=True, extra="allow")

    class UserResponse(_GraphModel):
        user_id: str = Field(alias="id")
        user_principal_name: Optional[str] = Field(default=None, alias="userPrincipalName")
        display_name: Optional[str] = Field(default=None, alias="displayName")
        mail: Optional[str] = None
        account_enabled: Optional[bool] = Field(default=None, alias="accountEnabled")
        job_title: Optional[str] = Field(default=None, alias="jobTitle")
        department: Optional[str] = None
        user_type: Optional[str] = Field(default=None, alias="userType")
        created_date_time: Optional[str] = Field(default=None, alias="createdDateTime")

    class GroupResponse(_GraphModel):
        group_id: str = Field(alias="id")
        display_name: Optional[str] = Field(default=None, alias="displayName")
        mail_nickname: Optional[str] = Field(default=None, alias="mailNickname")
        mail_enabled: Optional[bool] = Field(default=None, alias="mailEnabled")
        security_enabled: Optional[bool] = Field(default=None, alias="securityEnabled")
        description: Optional[str] = None
        group_types: List[str] = Field(default_factory=list, alias="groupTypes")

    class GraphPageResponse(_GraphModel):
        value: List[Dict[str, Any]] = Field(default_factory=list)
        next_link: Optional[str] = Field(default=None, alias="@odata.nextLink")
        odata_count: Optional[int] = Field(default=None, alias="@odata.count")

except ImportError:  # pragma: no cover — pydantic is pre-installed
    UserResponse = GroupResponse = GraphPageResponse = None  # type: ignore[assignment]


# ── Dataclass projections (import-safe even without pydantic) ────────────────


@dataclass
class EntraIdUser:
    """Lightweight projection of a Microsoft Graph /users object."""

    id: str
    user_principal_name: str = ""
    display_name: str = ""
    mail: str = ""
    account_enabled: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EntraIdGroup:
    """Lightweight projection of a Microsoft Graph /groups object."""

    id: str
    display_name: str = ""
    mail_nickname: str = ""
    security_enabled: bool = True
    mail_enabled: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphPage:
    """A page of Microsoft Graph results plus continuation token."""

    value: List[Dict[str, Any]] = field(default_factory=list)
    next_link: Optional[str] = None
    odata_count: Optional[int] = None
