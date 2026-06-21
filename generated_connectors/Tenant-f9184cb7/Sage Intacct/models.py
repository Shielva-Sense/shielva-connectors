"""Local dataclasses for the Sage Intacct connector.

These are *internal* shapes — the connector boundary still accepts/returns
plain ``Dict[str, Any]`` payloads so the gateway can introspect them
without importing this module. Use these classes for type-narrowing inside
the connector when you want clarity on what a slice of XML really carries.

The ``IntacctFunctionResult`` ``auth_status`` / ``health`` properties expose
stable string shims so callers that don't import the shared SDK enums get
a predictable API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class IntacctCredentials:
    """Bundle of all five required Sage Intacct credentials.

    Sage Intacct's XML gateway authenticates each request twice:

      * sender (Web Services partner) via ``<control>``
      * user (Intacct user with API privilege) via ``<operation><authentication>``
    """
    sender_id: str
    sender_password: str
    user_id: str
    user_password: str
    company_id: str
    location_id: Optional[str] = None
    entity_id: Optional[str] = None

    @property
    def is_complete(self) -> bool:
        """True iff all five mandatory credentials are present."""
        return bool(
            self.sender_id
            and self.sender_password
            and self.user_id
            and self.user_password
            and self.company_id
        )


@dataclass
class IntacctReadByQueryRequest:
    """Inputs to ``readByQuery``."""
    object_name: str
    fields: str = "*"
    query: Optional[str] = None
    pagesize: int = 100
    return_format: str = "json"


@dataclass
class IntacctReadRequest:
    """Inputs to ``read`` (by primary key)."""
    object_name: str
    keys: List[str] = field(default_factory=list)
    fields: str = "*"


@dataclass
class IntacctFunctionResult:
    """Normalised parsed result of a single Intacct ``<function>`` response."""
    controlid: str
    status: str               # "success" | "failure"
    function_name: str = ""
    data: List[Dict[str, Any]] = field(default_factory=list)
    result_id: Optional[str] = None
    num_remaining: int = 0
    total_count: int = 0
    error_no: Optional[str] = None
    error_description: Optional[str] = None
    error_correction: Optional[str] = None

    @property
    def auth_status(self) -> str:
        """Shim: surface AuthStatus-like string for callers that skip the SDK enum."""
        if self.status == "success":
            return "CONNECTED"
        if self.error_no and self.error_no.startswith("XL03"):
            # XL03* family covers Intacct auth / login / session failures
            return "TOKEN_EXPIRED"
        return "MISSING_CREDENTIALS"

    @property
    def health(self) -> str:
        """Shim: surface ConnectorHealth-like string."""
        return "HEALTHY" if self.status == "success" else "DEGRADED"


@dataclass
class IntacctSession:
    """Cached gateway session — minted by ``getAPISession`` at install time."""
    session_id: str
    endpoint: Optional[str] = None
