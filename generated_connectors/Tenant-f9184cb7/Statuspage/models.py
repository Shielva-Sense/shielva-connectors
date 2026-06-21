"""Pydantic request/response schemas for the Statuspage REST API.

snake_case attribute names match Statuspage's wire format. The connector
boundary itself passes ``Dict[str, Any]`` payloads — these models are reserved
for adapters that want type-checked construction (e.g. richer SDK wrappers).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _StatuspageModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


# ── Pages ─────────────────────────────────────────────────────────────────


class Page(_StatuspageModel):
    """Statuspage page resource — the root of every API surface."""

    page_id: str = Field(alias="id")
    name: Optional[str] = None
    page_description: Optional[str] = None
    url: Optional[str] = None
    domain: Optional[str] = None
    subdomain: Optional[str] = None
    time_zone: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ── Components ────────────────────────────────────────────────────────────


class Component(_StatuspageModel):
    """Statuspage component — one row on the public status page."""

    component_id: str = Field(alias="id")
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    group_id: Optional[str] = None
    showcase: Optional[bool] = None
    only_show_if_degraded: Optional[bool] = None
    page_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ComponentGroup(_StatuspageModel):
    """Grouping rendered as a single fold-out on the status page."""

    group_id: str = Field(alias="id")
    name: Optional[str] = None
    description: Optional[str] = None
    components: List[str] = Field(default_factory=list)


# ── Incidents ─────────────────────────────────────────────────────────────


class IncidentUpdate(_StatuspageModel):
    update_id: str = Field(alias="id")
    body: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Incident(_StatuspageModel):
    incident_id: str = Field(alias="id")
    name: Optional[str] = None
    status: Optional[str] = None
    impact: Optional[str] = None
    impact_override: Optional[str] = None
    shortlink: Optional[str] = None
    monitoring_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    page_id: Optional[str] = None
    incident_updates: List[IncidentUpdate] = Field(
        default_factory=list, alias="incident_updates"
    )
    components: List[Dict[str, Any]] = Field(default_factory=list)


class Maintenance(_StatuspageModel):
    """Scheduled maintenance window."""

    maintenance_id: str = Field(alias="id")
    name: Optional[str] = None
    status: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    scheduled_until: Optional[datetime] = None
    impact: Optional[str] = None


# ── Subscribers ───────────────────────────────────────────────────────────


class Subscriber(_StatuspageModel):
    """Email / SMS / restricted-page subscriber record."""

    subscriber_id: str = Field(alias="id")
    email: Optional[str] = None
    phone_number: Optional[str] = None
    phone_country: Optional[str] = None
    skip_confirmation_notification: Optional[bool] = None
    mode: Optional[str] = None
    state: Optional[str] = None


# ── Metrics ───────────────────────────────────────────────────────────────


class Metric(_StatuspageModel):
    metric_id: str = Field(alias="id")
    name: Optional[str] = None
    metric_identifier: Optional[str] = None
    display: Optional[bool] = None
    suffix: Optional[str] = None
    y_axis_min: Optional[float] = None
    y_axis_max: Optional[float] = None
