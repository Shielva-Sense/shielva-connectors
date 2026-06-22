"""Pydantic request/response schemas for Kommo REST APIs.

snake_case field names match Kommo's wire format; the connector boundary uses
``Dict[str, Any]`` payloads — these models are reserved for callers that want
strongly-typed shapes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _KommoModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_KommoModel):
    page: int = 1
    limit: int = 50


class LeadResponse(_KommoModel):
    id: int
    name: Optional[str] = None
    price: Optional[int] = None
    pipeline_id: Optional[int] = None
    status_id: Optional[int] = None
    responsible_user_id: Optional[int] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class ContactResponse(_KommoModel):
    id: int
    name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    responsible_user_id: Optional[int] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class CompanyResponse(_KommoModel):
    id: int
    name: Optional[str] = None
    responsible_user_id: Optional[int] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class TaskResponse(_KommoModel):
    id: int
    text: Optional[str] = None
    task_type_id: Optional[int] = None
    responsible_user_id: Optional[int] = None
    complete_till: Optional[int] = None
    is_completed: bool = False
    entity_id: Optional[int] = None
    entity_type: Optional[str] = None


class PageResult(_KommoModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    page: int = 1
    next_link: Optional[str] = None
