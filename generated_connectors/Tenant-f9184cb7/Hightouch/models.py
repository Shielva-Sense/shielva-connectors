"""Local dataclasses for the Hightouch connector.

The connector boundary uses ``Dict[str, Any]`` payloads (matching the wire
format). These dataclasses are provided for callers who want typed handles
on the most-used shapes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ── Core resources ──────────────────────────────────────────────────────────


@dataclass
class HightouchWorkspace:
    """A Hightouch workspace — top-level org container."""

    id: int
    name: str
    slug: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.slug or f"workspace-{self.id}"


@dataclass
class HightouchSource:
    """A Hightouch source (warehouse / DB connection)."""

    id: int
    name: str
    slug: str = ""
    type: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.slug or f"source-{self.id}"


@dataclass
class HightouchDestination:
    """A Hightouch destination (SaaS tool data is activated into)."""

    id: int
    name: str
    slug: str = ""
    type: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.slug or f"destination-{self.id}"


@dataclass
class HightouchModel:
    """A Hightouch model — a SQL / audience definition over a source."""

    id: int
    name: str
    slug: str = ""
    source_id: Optional[int] = None
    primary_key: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.slug or f"model-{self.id}"


@dataclass
class HightouchSync:
    """A Hightouch sync (model → destination pipeline)."""

    id: int
    slug: str = ""
    model_id: Optional[int] = None
    destination_id: Optional[int] = None
    disabled: bool = False
    schedule: Optional[Dict[str, Any]] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.slug or f"sync-{self.id}"

    @property
    def is_enabled(self) -> bool:
        return not bool(self.disabled)


@dataclass
class HightouchSyncRun:
    """A single execution attempt of a sync."""

    id: int
    sync_id: int
    status: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status.lower() in ("success", "succeeded", "completed")


@dataclass
class HightouchSequence:
    """A Hightouch sequence — orchestrated multi-step pipeline."""

    id: int
    name: str
    slug: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HightouchTriggerResult:
    """Response returned by POST /syncs/{id}/trigger."""

    sync_id: int
    sync_run_id: Optional[int] = None
    accepted: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.accepted
