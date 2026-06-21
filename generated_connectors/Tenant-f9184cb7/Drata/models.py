"""Pydantic request/response schemas for Drata REST APIs.

camelCase aliases match Drata's wire format; the connector boundary uses
Dict[str, Any] payloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _DrataModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_DrataModel):
    limit: int = 100
    offset: int = 0


class PersonnelResponse(_DrataModel):
    personnel_id: str = Field(alias="id")
    first_name: Optional[str] = Field(default=None, alias="firstName")
    last_name: Optional[str] = Field(default=None, alias="lastName")
    email: Optional[str] = None
    role: Optional[str] = None
    status: Optional[str] = None
    employment_type: Optional[str] = Field(default=None, alias="employmentType")
    created_at: Optional[datetime] = Field(default=None, alias="createdAt")
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")


class ControlResponse(_DrataModel):
    control_id: str = Field(alias="id")
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    framework_ids: List[str] = Field(default_factory=list, alias="frameworkIds")
    owner: Optional[str] = None
    created_at: Optional[datetime] = Field(default=None, alias="createdAt")
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")


class EvidenceResponse(_DrataModel):
    evidence_id: str = Field(alias="id")
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    control_ids: List[str] = Field(default_factory=list, alias="controlIds")


class RiskResponse(_DrataModel):
    risk_id: str = Field(alias="id")
    name: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[str] = None
    likelihood: Optional[str] = None
    status: Optional[str] = None


class VendorResponse(_DrataModel):
    vendor_id: str = Field(alias="id")
    name: Optional[str] = None
    category: Optional[str] = None
    risk_level: Optional[str] = Field(default=None, alias="riskLevel")
    status: Optional[str] = None


class PageResult(_DrataModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    total: Optional[int] = None
    next_offset: Optional[int] = Field(default=None, alias="nextOffset")
