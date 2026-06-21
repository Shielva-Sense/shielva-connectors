"""Pydantic / dataclass request + reference schemas for ADP REST APIs.

ADP wire format is **camelCase** (`associateOID`, `payStatementID`,
`workAssignments`). At the connector boundary we pass dicts through unchanged
— these schemas exist for type hints, validation in tests, and as a
documentation surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _ADPModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ODataPaging(_ADPModel):
    """`$top` / `$skip` pagination (ADP uses OData v4)."""

    top: int = 100
    skip: int = 0


class WorkerResponse(_ADPModel):
    associate_oid: str = Field(alias="associateOID")
    person: Optional[Dict[str, Any]] = None
    work_assignments: List[Dict[str, Any]] = Field(default_factory=list, alias="workAssignments")
    worker_status: Optional[Dict[str, Any]] = Field(default=None, alias="workerStatus")


class PayStatementResponse(_ADPModel):
    pay_statement_id: str = Field(alias="payStatementID")
    pay_date: Optional[str] = Field(default=None, alias="payDate")
    net_pay_amount: Optional[Dict[str, Any]] = Field(default=None, alias="netPayAmount")
    gross_pay_amount: Optional[Dict[str, Any]] = Field(default=None, alias="grossPayAmount")
    earnings: List[Dict[str, Any]] = Field(default_factory=list)
    deductions: List[Dict[str, Any]] = Field(default_factory=list)


class TimeOffRequestPayload(BaseModel):
    """Validated body for submit_time_off_request().

    This is a plain pydantic model (not _ADPModel) because the caller hands us
    snake_case kwargs and we serialize to the camelCase Events envelope via
    `helpers/utils.py::build_time_off_event`.
    """

    worker_aoid: str
    policy_code: str
    start_date: str
    end_date: str
    hours: Optional[float] = None
    comments: Optional[str] = None


@dataclass
class ADPWorkerRef:
    """Lightweight worker reference (used in tests + opt-in normalizers)."""

    aoid: str
    associate_oid: Optional[str] = None
    work_assignment_status: Optional[str] = None
    legal_name: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:  # noqa: A003
        return self.aoid


@dataclass
class ADPPayStatementRef:
    """Lightweight pay-statement reference."""

    pay_statement_id: str
    pay_date: Optional[str] = None
    net_pay_amount: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:  # noqa: A003
        return self.pay_statement_id
