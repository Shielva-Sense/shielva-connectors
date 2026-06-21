"""Local dataclasses for the Rudderstack connector.

The connector boundary uses ``Dict[str, Any]`` payloads (matching the wire
format). These dataclasses are provided for callers who want typed handles
on the most-used shapes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Control-plane resources ──────────────────────────────────────────────────


@dataclass
class RudderstackSource:
    """A Rudderstack source (a stream of events)."""

    id: str
    name: str
    type: str
    config: Dict[str, Any] = field(default_factory=dict)
    write_key: Optional[str] = None
    enabled: bool = True
    workspace_id: Optional[str] = None

    @property
    def source_id(self) -> str:
        return self.id


@dataclass
class RudderstackDestination:
    """A Rudderstack destination (where events are forwarded)."""

    id: str
    name: str
    type: str
    config: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    source_id: Optional[str] = None

    @property
    def destination_id(self) -> str:
        return self.id


@dataclass
class RudderstackConnection:
    """A connection wiring a source to a destination."""

    id: str
    source_id: str
    destination_id: str
    enabled: bool = True

    @property
    def connection_id(self) -> str:
        return self.id


@dataclass
class RudderstackWorkspace:
    """A Rudderstack workspace — top-level org container."""

    id: str
    name: str
    region: Optional[str] = None


@dataclass
class RudderstackProfile:
    """A unified user profile from the Profiles API."""

    id: str
    traits: Dict[str, Any] = field(default_factory=dict)
    identities: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class RudderstackIdentity:
    """A single identity record (one of many tied to a profile)."""

    id: str
    profile_id: str
    type: str
    value: str


# ── Data-plane event envelopes ───────────────────────────────────────────────


@dataclass
class TrackEvent:
    """A Rudderstack ``track`` event payload."""

    user_id: str
    event: str
    properties: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "userId": self.user_id,
            "event": self.event,
            "properties": self.properties,
        }
        if self.timestamp:
            payload["timestamp"] = self.timestamp
        if self.context:
            payload["context"] = self.context
        return payload


@dataclass
class IdentifyEvent:
    """A Rudderstack ``identify`` event payload."""

    user_id: str
    traits: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"userId": self.user_id, "traits": self.traits}
        if self.timestamp:
            payload["timestamp"] = self.timestamp
        return payload


@dataclass
class PageEvent:
    """A Rudderstack ``page`` event payload."""

    user_id: str
    name: Optional[str] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None


@dataclass
class ScreenEvent:
    """A Rudderstack ``screen`` event payload (mobile)."""

    user_id: str
    name: Optional[str] = None
    properties: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None


@dataclass
class GroupEvent:
    """A Rudderstack ``group`` event payload."""

    user_id: str
    group_id: str
    traits: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None


@dataclass
class AliasEvent:
    """A Rudderstack ``alias`` event payload."""

    user_id: str
    previous_id: str
    timestamp: Optional[str] = None
