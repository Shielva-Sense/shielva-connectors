"""Pydantic request/response schemas for OneSignal REST APIs.

snake_case keys match OneSignal wire format; the connector boundary uses
``Dict[str, Any]`` payloads. These models exist for documentation / future
typed-builder use; the public connector API remains dict-based for OCP.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _OneSignalModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Paging(_OneSignalModel):
    limit: int = 50
    offset: int = 0


class NotificationRequest(_OneSignalModel):
    """POST /notifications body."""

    app_id: str
    contents: Dict[str, str]
    headings: Optional[Dict[str, str]] = None
    included_segments: Optional[List[str]] = None
    excluded_segments: Optional[List[str]] = None
    include_player_ids: Optional[List[str]] = None
    include_external_user_ids: Optional[List[str]] = None
    data: Optional[Dict[str, Any]] = None
    url: Optional[str] = None
    big_picture: Optional[str] = None
    send_after: Optional[str] = None


class NotificationResponse(_OneSignalModel):
    id: str
    recipients: Optional[int] = None
    external_id: Optional[str] = None
    errors: Optional[Any] = None


class AppResponse(_OneSignalModel):
    id: str
    name: str
    players: Optional[int] = None
    messageable_players: Optional[int] = None
    updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    gcm_key: Optional[str] = None
    apns_env: Optional[str] = None


class PlayerResponse(_OneSignalModel):
    id: str
    app_id: Optional[str] = None
    identifier: Optional[str] = None
    device_type: Optional[int] = None
    language: Optional[str] = None
    tags: Dict[str, Any] = Field(default_factory=dict)
    external_user_id: Optional[str] = None


class SegmentRequest(_OneSignalModel):
    name: str
    filters: List[Dict[str, Any]]


class PageResult(_OneSignalModel):
    total_count: Optional[int] = None
    offset: Optional[int] = None
    limit: Optional[int] = None
    items: List[Dict[str, Any]] = Field(default_factory=list)
