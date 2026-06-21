"""Local dataclass models for the Plausible Analytics connector.

These mirror the shape of the upstream Plausible JSON responses so the
connector can pass them around with type safety. Property shims are exposed
on the install/health-check surfaces so callers can read either the dataclass
attribute or the legacy `.auth_status` / `.health` aliases without conversion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Plausible response shapes ──────────────────────────────────────────────


@dataclass
class AggregateMetric:
    """A single aggregate metric value (with optional comparison change)."""

    value: float = 0.0
    change: Optional[float] = None


@dataclass
class AggregateResult:
    """Result of /api/v1/stats/aggregate — map of metric name → AggregateMetric."""

    results: Dict[str, AggregateMetric] = field(default_factory=dict)

    @classmethod
    def from_response(cls, data: Dict[str, Any]) -> "AggregateResult":
        raw = data.get("results", {}) or {}
        out: Dict[str, AggregateMetric] = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                out[k] = AggregateMetric(
                    value=float(v.get("value", 0) or 0),
                    change=v.get("change"),
                )
        return cls(results=out)


@dataclass
class TimeseriesPoint:
    """A single time-bucketed datapoint from /api/v1/stats/timeseries."""

    date: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BreakdownRow:
    """A single row from /api/v1/stats/breakdown — dimension + metric values."""

    dimension: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RealtimeVisitors:
    """Result of /api/v1/stats/realtime/visitors — integer count."""

    visitors: int = 0


@dataclass
class Site:
    """Plausible site resource (id == primary domain)."""

    domain: str = ""
    timezone: str = "UTC"
    custom_properties: List[str] = field(default_factory=list)


@dataclass
class Goal:
    """Plausible conversion goal — either an event or a page-path goal."""

    id: Optional[str] = None
    goal_type: str = "event"   # "event" | "page"
    event_name: Optional[str] = None
    page_path: Optional[str] = None


# ── Connector status shims ─────────────────────────────────────────────────


@dataclass
class _AuthStatusShim:
    """Convenience wrapper that exposes a `.value` *and* shim properties.

    The platform passes the canonical shared.base_connector.AuthStatus enum
    everywhere; these local shims exist so connector-internal code paths can
    read the same `auth_status` / `health` attributes without importing the
    shared enum at every call site.
    """

    value: str = "pending"

    @property
    def auth_status(self) -> str:  # noqa: D401 — shim property
        return self.value


@dataclass
class _ConnectorHealthShim:
    """Same shape as `_AuthStatusShim` but for connector health."""

    value: str = "healthy"

    @property
    def health(self) -> str:  # noqa: D401 — shim property
        return self.value
