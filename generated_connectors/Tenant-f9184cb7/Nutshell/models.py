"""Typed dataclasses for the Nutshell connector.

The connector boundary trades plain dicts so multi-tenant gateways and the
ACP runtime can serialise everything without knowing about these classes; the
dataclasses live here purely as a SOC seam between raw Nutshell JSON-RPC
responses (heavy nesting, camelCase keys) and the normalised flat dicts
returned by ``helpers/normalizer.py``.

Auth / health / sync result envelopes are NOT redefined here — they come from
``shared.base_connector`` (`ConnectorStatus`, `AuthStatus`, `ConnectorHealth`,
`SyncResult`, `SyncStatus`, `TokenInfo`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Generic list-API result envelope (contacts / leads / accounts / activities)
# ---------------------------------------------------------------------------


@dataclass
class NutshellListResult:
    """Generic paged-list result envelope."""

    items: List[Dict[str, Any]] = field(default_factory=list)
    page: int = 1
    limit: int = 50
    has_more: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": self.items,
            "page": self.page,
            "limit": self.limit,
            "has_more": self.has_more,
        }


# ---------------------------------------------------------------------------
# Nutshell-specific typed wrappers
# ---------------------------------------------------------------------------


@dataclass
class NutshellContact:
    """Thin typed wrapper around a Nutshell Contact JSON-RPC record."""

    id: int
    rev: str = ""
    name: str = ""
    first_name: str = ""
    last_name: str = ""
    emails: List[Dict[str, Any]] = field(default_factory=list)
    phones: List[Dict[str, Any]] = field(default_factory=list)
    accounts: List[Dict[str, Any]] = field(default_factory=list)
    custom_fields: Dict[str, Any] = field(default_factory=dict)
    created_time: Optional[str] = None
    modified_time: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "NutshellContact":
        name_obj = raw.get("name") or {}
        if isinstance(name_obj, dict):
            display = name_obj.get("displayName") or ""
            first = name_obj.get("givenName") or ""
            last = name_obj.get("familyName") or ""
        else:
            display = str(name_obj)
            first = ""
            last = ""
        return cls(
            id=int(raw.get("id") or 0),
            rev=str(raw.get("rev") or ""),
            name=display,
            first_name=first,
            last_name=last,
            emails=raw.get("email") or [],
            phones=raw.get("phone") or [],
            accounts=raw.get("accounts") or [],
            custom_fields=raw.get("customFields") or {},
            created_time=raw.get("createdTime"),
            modified_time=raw.get("modifiedTime"),
            raw=raw,
        )


@dataclass
class NutshellLead:
    """Thin typed wrapper around a Nutshell Lead JSON-RPC record."""

    id: int
    rev: str = ""
    description: str = ""
    confidence: int = 0
    value: Dict[str, Any] = field(default_factory=dict)
    status: int = 0
    primary_account: Dict[str, Any] = field(default_factory=dict)
    contacts: List[Dict[str, Any]] = field(default_factory=list)
    created_time: Optional[str] = None
    modified_time: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "NutshellLead":
        return cls(
            id=int(raw.get("id") or 0),
            rev=str(raw.get("rev") or ""),
            description=raw.get("description") or "",
            confidence=int(raw.get("confidence") or 0),
            value=raw.get("value") or {},
            status=int(raw.get("status") or 0),
            primary_account=raw.get("primaryAccount") or {},
            contacts=raw.get("contacts") or [],
            created_time=raw.get("createdTime"),
            modified_time=raw.get("modifiedTime"),
            raw=raw,
        )


@dataclass
class NutshellAccount:
    """Thin typed wrapper around a Nutshell Account JSON-RPC record."""

    id: int
    rev: str = ""
    name: str = ""
    industry: str = ""
    territory: str = ""
    created_time: Optional[str] = None
    modified_time: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "NutshellAccount":
        industry_raw = raw.get("industry")
        if isinstance(industry_raw, dict):
            industry = industry_raw.get("name", "") or ""
        else:
            industry = industry_raw or ""
        territory_raw = raw.get("territory")
        if isinstance(territory_raw, dict):
            territory = territory_raw.get("name", "") or ""
        else:
            territory = territory_raw or ""
        return cls(
            id=int(raw.get("id") or 0),
            rev=str(raw.get("rev") or ""),
            name=raw.get("name") or "",
            industry=industry,
            territory=territory,
            created_time=raw.get("createdTime"),
            modified_time=raw.get("modifiedTime"),
            raw=raw,
        )
