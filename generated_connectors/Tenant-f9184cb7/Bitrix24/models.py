"""Pydantic request/response schemas for the Bitrix24 REST API.

Bitrix24 wire format is UPPERCASE_SNAKE on CRM entities (`ID`, `TITLE`,
`STAGE_ID`) and lowerCamelCase on Tasks (`id`, `title`, `responsibleId`).
The connector boundary uses `Dict[str, Any]` payloads — these models exist
for tests + future typed callers.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Bitrix24Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ListPageRequest(_Bitrix24Model):
    """Generic CRM list page request."""
    start: int = 0
    select: List[str] = Field(default_factory=lambda: ["*"])
    filter: Dict[str, Any] = Field(default_factory=dict)
    order: Dict[str, str] = Field(default_factory=lambda: {"ID": "ASC"})


class LeadFields(_Bitrix24Model):
    title: Optional[str] = Field(default=None, alias="TITLE")
    name: Optional[str] = Field(default=None, alias="NAME")
    last_name: Optional[str] = Field(default=None, alias="LAST_NAME")
    status_id: Optional[str] = Field(default=None, alias="STATUS_ID")
    source_id: Optional[str] = Field(default=None, alias="SOURCE_ID")
    opportunity: Optional[float] = Field(default=None, alias="OPPORTUNITY")
    currency_id: Optional[str] = Field(default=None, alias="CURRENCY_ID")


class ContactFields(_Bitrix24Model):
    name: Optional[str] = Field(default=None, alias="NAME")
    last_name: Optional[str] = Field(default=None, alias="LAST_NAME")
    phone: List[Dict[str, Any]] = Field(default_factory=list, alias="PHONE")
    email: List[Dict[str, Any]] = Field(default_factory=list, alias="EMAIL")


class DealFields(_Bitrix24Model):
    title: Optional[str] = Field(default=None, alias="TITLE")
    contact_id: Optional[int] = Field(default=None, alias="CONTACT_ID")
    company_id: Optional[int] = Field(default=None, alias="COMPANY_ID")
    stage_id: Optional[str] = Field(default=None, alias="STAGE_ID")
    opportunity: Optional[float] = Field(default=None, alias="OPPORTUNITY")
    currency_id: Optional[str] = Field(default=None, alias="CURRENCY_ID")


class TaskFields(_Bitrix24Model):
    title: Optional[str] = None
    description: Optional[str] = None
    responsible_id: Optional[int] = Field(default=None, alias="responsibleId")
    deadline: Optional[str] = None


class Bitrix24RestResult(_Bitrix24Model):
    """Bitrix24 REST envelope: `{result, total?, next?, time?}`."""
    result: Any
    total: Optional[int] = None
    next: Optional[int] = None
    time: Optional[Dict[str, Any]] = None
