"""Local dataclasses + property shims for the ADP connector.

These mirror the public shape of `shared.base_connector.AuthStatus` and
`shared.base_connector.ConnectorHealth` so callers that import from this module
get a stable surface even if the shared package is unavailable at import time.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AuthStatusShim:
    """Local mirror of `shared.base_connector.AuthStatus` values."""

    value: str

    @property
    def authenticated(self) -> bool:
        return self.value in {"connected", "authenticated"}

    @property
    def needs_credentials(self) -> bool:
        return self.value == "missing_credentials"


@dataclass
class ConnectorHealthShim:
    """Local mirror of `shared.base_connector.ConnectorHealth` values."""

    value: str

    @property
    def healthy(self) -> bool:
        return self.value == "healthy"

    @property
    def offline(self) -> bool:
        return self.value == "offline"


@dataclass
class ADPWorkerRef:
    """Lightweight worker reference returned by /hr/v2/workers listings."""

    aoid: str
    associate_oid: Optional[str] = None
    work_assignment_status: Optional[str] = None
    legal_name: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.aoid


@dataclass
class ADPPayStatementRef:
    """Lightweight pay-statement reference."""

    pay_statement_id: str
    pay_date: Optional[str] = None
    net_pay_amount: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.pay_statement_id


@dataclass
class TimeOffRequestPayload:
    """Validated body for submit_time_off_request()."""

    worker_aoid: str
    policy_code: str
    start_date: str
    end_date: str
    hours: Optional[float] = None
    comments: Optional[str] = None

    def to_event_body(self) -> Dict[str, Any]:
        """Serialize to the ADP Events API envelope shape."""
        body: Dict[str, Any] = {
            "events": [
                {
                    "data": {
                        "eventContext": {
                            "worker": {"associateOID": self.worker_aoid},
                        },
                        "transform": {
                            "timeOffRequest": {
                                "timeOffPolicyCode": {"codeValue": self.policy_code},
                                "startDate": self.start_date,
                                "endDate": self.end_date,
                            }
                        },
                    }
                }
            ]
        }
        tor = body["events"][0]["data"]["transform"]["timeOffRequest"]
        if self.hours is not None:
            tor["totalTimeOffHours"] = self.hours
        if self.comments:
            tor["comments"] = [{"textValue": self.comments}]
        return body
