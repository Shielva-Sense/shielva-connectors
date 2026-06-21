"""Pydantic request/response schemas for HiBob REST APIs.

camelCase aliases match the HiBob wire format; the connector boundary uses
``Dict[str, Any]`` payloads for flexibility — these models are reference
shapes for callers + documentation aid.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _HiBobModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class WorkInfo(_HiBobModel):
    title: Optional[str] = None
    department: Optional[str] = None
    site: Optional[str] = None
    start_date: Optional[str] = Field(default=None, alias="startDate")
    company_id: Optional[str] = Field(default=None, alias="companyId")
    manager: Optional[str] = None


class CreateEmployeeBody(_HiBobModel):
    """Body shape for ``POST /people``."""

    first_name: str = Field(alias="firstName")
    surname: str
    email: str
    work_email: Optional[str] = Field(default=None, alias="workEmail")
    work: Optional[WorkInfo] = None


class TimeOffRequestBody(_HiBobModel):
    """Body shape for ``POST /timeoff/employees/{id}/requests``."""

    policy_type_display_name: str = Field(alias="policyTypeDisplayName")
    request_range_type: str = Field(alias="requestRangeType")
    start_date: str = Field(alias="startDate")
    end_date: Optional[str] = Field(default=None, alias="endDate")
    description: Optional[str] = None


class PeopleSearchBody(_HiBobModel):
    """Body shape for ``POST /people/search``."""

    fields: List[str] = Field(default_factory=list)
    filters: List[Dict[str, Any]] = Field(default_factory=list)


class EmployeeResponse(_HiBobModel):
    employee_id: str = Field(alias="id")
    first_name: Optional[str] = Field(default=None, alias="firstName")
    surname: Optional[str] = None
    display_name: Optional[str] = Field(default=None, alias="displayName")
    email: Optional[str] = None
    work_email: Optional[str] = Field(default=None, alias="workEmail")
    start_date: Optional[str] = Field(default=None, alias="startDate")
    modification_date: Optional[datetime] = Field(default=None, alias="modificationDate")
    work: Optional[Dict[str, Any]] = None


class LifecycleChangeResponse(_HiBobModel):
    employee_id: str = Field(alias="employeeId")
    status: Optional[str] = None
    effective_date: Optional[str] = Field(default=None, alias="effectiveDate")
    reason: Optional[str] = None


class PageResult(_HiBobModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_cursor: Optional[str] = None
