"""Local dataclasses + shim properties for the Plivo connector.

These models mirror the shapes the connector returns from Plivo REST endpoints
so callers can rely on attribute access without importing the shared
``shared.base_connector`` enums directly. Each shim exposes the equivalent
``AuthStatus`` / ``ConnectorHealth`` value as a ``@property`` for ergonomic
read-only access.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Auth / health shims ────────────────────────────────────────────────────


@dataclass
class PlivoAuthStatus:
    """Lightweight shim around the connector's auth status string.

    The connector uses ``shared.base_connector.AuthStatus`` for the canonical
    enum; this shim is a stable dataclass that downstream tools (CLI, tests,
    serializers) can consume without importing the platform package.
    """

    value: str

    @property
    def authenticated(self) -> bool:
        return self.value in ("connected", "authenticated")

    @property
    def expired(self) -> bool:
        return self.value in ("token_expired", "expired", "failed")


@dataclass
class PlivoConnectorHealth:
    """Shim for the connector's health state."""

    value: str

    @property
    def healthy(self) -> bool:
        return self.value == "healthy"

    @property
    def degraded(self) -> bool:
        return self.value == "degraded"

    @property
    def offline(self) -> bool:
        return self.value == "offline"


# ── Plivo resource shapes ──────────────────────────────────────────────────


@dataclass
class PlivoAccount:
    """Plivo account resource — GET /Account/{auth_id}/."""

    auth_id: str = ""
    name: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    cash_credits: str = ""
    auto_recharge: bool = False
    timezone: str = ""
    billing_mode: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return bool(self.auth_id)


@dataclass
class PlivoMessage:
    """Plivo message resource — GET /Message/{uuid}."""

    message_uuid: str = ""
    from_number: str = ""
    to_number: str = ""
    message_state: str = ""
    message_direction: str = ""
    message_type: str = ""
    message_time: str = ""
    total_amount: str = ""
    total_rate: str = ""
    units: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def delivered(self) -> bool:
        return self.message_state in ("delivered", "sent")


@dataclass
class PlivoCall:
    """Plivo call resource — GET /Call/{uuid}."""

    call_uuid: str = ""
    from_number: str = ""
    to_number: str = ""
    call_direction: str = ""
    call_status: str = ""
    call_duration: int = 0
    answer_time: str = ""
    end_time: str = ""
    total_amount: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.call_status in ("completed", "answered")


@dataclass
class PlivoPhoneNumber:
    """Plivo phone number resource — GET /Number/{number} or /PhoneNumber/."""

    number: str = ""
    country: str = ""
    region: str = ""
    type: str = ""
    monthly_rental_rate: str = ""
    voice_enabled: bool = False
    sms_enabled: bool = False
    application: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlivoApplication:
    """Plivo voice application — GET /Application/{id}."""

    app_id: str = ""
    app_name: str = ""
    answer_url: str = ""
    answer_method: str = "POST"
    hangup_url: Optional[str] = None
    message_url: Optional[str] = None
    message_method: str = "POST"
    public_uri: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlivoListResponse:
    """Generic paginated list response — Plivo wraps lists in {meta, objects}."""

    api_id: str = ""
    objects: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.objects)

    @property
    def total_count(self) -> int:
        return int(self.meta.get("total_count", 0))

    @property
    def next_url(self) -> Optional[str]:
        return self.meta.get("next")
