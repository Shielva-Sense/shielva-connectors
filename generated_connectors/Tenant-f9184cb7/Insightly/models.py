"""Pydantic request/response schemas for Insightly REST APIs.

UPPER_SNAKE_CASE field names match Insightly's wire format; the connector
boundary uses Dict[str, Any] payloads. These models are exported for callers
that want type-safe envelopes without re-importing the SDK enums.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _InsightlyModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_InsightlyModel):
    """OData-style pagination."""

    top: int = 50
    skip: int = 0


class EmailAddress(_InsightlyModel):
    email_address: Optional[str] = Field(default=None, alias="EMAIL_ADDRESS")


class ContactInfo(_InsightlyModel):
    type: Optional[str] = Field(default=None, alias="TYPE")  # PHONE / WEBSITE / SOCIAL
    label: Optional[str] = Field(default=None, alias="LABEL")
    detail: Optional[str] = Field(default=None, alias="DETAIL")


class ContactResponse(_InsightlyModel):
    contact_id: Optional[int] = Field(default=None, alias="CONTACT_ID")
    first_name: Optional[str] = Field(default=None, alias="FIRST_NAME")
    last_name: Optional[str] = Field(default=None, alias="LAST_NAME")
    background: Optional[str] = Field(default=None, alias="BACKGROUND")
    organisation_id: Optional[int] = Field(default=None, alias="ORGANISATION_ID")
    email_addresses: List[EmailAddress] = Field(default_factory=list, alias="EMAILADDRESSES")
    contact_infos: List[ContactInfo] = Field(default_factory=list, alias="CONTACTINFOS")
    date_created_utc: Optional[datetime] = Field(default=None, alias="DATE_CREATED_UTC")
    date_updated_utc: Optional[datetime] = Field(default=None, alias="DATE_UPDATED_UTC")


class OrganisationResponse(_InsightlyModel):
    organisation_id: Optional[int] = Field(default=None, alias="ORGANISATION_ID")
    organisation_name: Optional[str] = Field(default=None, alias="ORGANISATION_NAME")
    phone: Optional[str] = Field(default=None, alias="PHONE")
    website: Optional[str] = Field(default=None, alias="WEBSITE")
    background: Optional[str] = Field(default=None, alias="BACKGROUND")
    date_created_utc: Optional[datetime] = Field(default=None, alias="DATE_CREATED_UTC")
    date_updated_utc: Optional[datetime] = Field(default=None, alias="DATE_UPDATED_UTC")


class OpportunityResponse(_InsightlyModel):
    opportunity_id: Optional[int] = Field(default=None, alias="OPPORTUNITY_ID")
    opportunity_name: Optional[str] = Field(default=None, alias="OPPORTUNITY_NAME")
    opportunity_value: Optional[float] = Field(default=None, alias="OPPORTUNITY_VALUE")
    probability: Optional[int] = Field(default=None, alias="PROBABILITY")
    bid_currency: Optional[str] = Field(default=None, alias="BID_CURRENCY")
    stage_id: Optional[int] = Field(default=None, alias="STAGE_ID")
    pipeline_id: Optional[int] = Field(default=None, alias="PIPELINE_ID")
    forecast_close_date: Optional[str] = Field(default=None, alias="FORECAST_CLOSE_DATE")
    date_created_utc: Optional[datetime] = Field(default=None, alias="DATE_CREATED_UTC")
    date_updated_utc: Optional[datetime] = Field(default=None, alias="DATE_UPDATED_UTC")


class LeadResponse(_InsightlyModel):
    lead_id: Optional[int] = Field(default=None, alias="LEAD_ID")
    first_name: Optional[str] = Field(default=None, alias="FIRST_NAME")
    last_name: Optional[str] = Field(default=None, alias="LAST_NAME")
    email: Optional[str] = Field(default=None, alias="EMAIL")
    organisation_name: Optional[str] = Field(default=None, alias="ORGANISATION_NAME")
    lead_status_id: Optional[int] = Field(default=None, alias="LEAD_STATUS_ID")
    lead_source_id: Optional[int] = Field(default=None, alias="LEAD_SOURCE_ID")
    date_created_utc: Optional[datetime] = Field(default=None, alias="DATE_CREATED_UTC")
    date_updated_utc: Optional[datetime] = Field(default=None, alias="DATE_UPDATED_UTC")


class ProjectResponse(_InsightlyModel):
    project_id: Optional[int] = Field(default=None, alias="PROJECT_ID")
    project_name: Optional[str] = Field(default=None, alias="PROJECT_NAME")
    status: Optional[str] = Field(default=None, alias="STATUS")
    date_created_utc: Optional[datetime] = Field(default=None, alias="DATE_CREATED_UTC")
    date_updated_utc: Optional[datetime] = Field(default=None, alias="DATE_UPDATED_UTC")


class TaskResponse(_InsightlyModel):
    task_id: Optional[int] = Field(default=None, alias="TASK_ID")
    title: Optional[str] = Field(default=None, alias="TITLE")
    status: Optional[str] = Field(default=None, alias="STATUS")
    priority: Optional[int] = Field(default=None, alias="PRIORITY")
    due_date: Optional[str] = Field(default=None, alias="DUE_DATE")


class PageResult(_InsightlyModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_skip: Optional[int] = None


__all__ = [
    "Paging",
    "EmailAddress",
    "ContactInfo",
    "ContactResponse",
    "OrganisationResponse",
    "OpportunityResponse",
    "LeadResponse",
    "ProjectResponse",
    "TaskResponse",
    "PageResult",
]
