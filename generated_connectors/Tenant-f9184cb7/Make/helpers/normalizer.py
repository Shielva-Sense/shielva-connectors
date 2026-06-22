"""Normalize Make API payloads → local dataclass models + NormalizedDocument.

The local model shims (``MakeOrganization``, ``MakeTeam``, …) are kept for
callers that want strongly-typed handles to Make resources. The
``normalize_scenario_document`` helper is what ``sync()`` uses to push
scenarios into the Shielva KB.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List

from models import (
    MakeExecution,
    MakeHook,
    MakeOrganization,
    MakeScenario,
    MakeTeam,
)


def _parse_dt(value: Any) -> datetime:
    """Best-effort RFC-3339 / ISO-8601 parser; falls back to *now*."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


# ── Dataclass normalisers (local models) ───────────────────────────────────

def normalize_organization(raw: Dict[str, Any]) -> MakeOrganization:
    return MakeOrganization(
        id=int(raw.get("id", 0)),
        name=str(raw.get("name", "")),
        raw=raw,
    )


def normalize_team(raw: Dict[str, Any]) -> MakeTeam:
    return MakeTeam(
        id=int(raw.get("id", 0)),
        name=str(raw.get("name", "")),
        organization_id=raw.get("organizationId"),
        raw=raw,
    )


def normalize_scenario(raw: Dict[str, Any]) -> MakeScenario:
    return MakeScenario(
        id=int(raw.get("id", 0)),
        name=str(raw.get("name", "")),
        team_id=raw.get("teamId"),
        is_active=bool(raw.get("isActive") or raw.get("isPaused") is False),
        raw=raw,
    )


def normalize_execution(raw: Dict[str, Any]) -> MakeExecution:
    return MakeExecution(
        id=str(raw.get("id", "")),
        scenario_id=raw.get("scenarioId"),
        status=raw.get("status"),
        raw=raw,
    )


def normalize_hook(raw: Dict[str, Any]) -> MakeHook:
    return MakeHook(
        id=int(raw.get("id", 0)),
        name=str(raw.get("name", "")),
        type_name=str(raw.get("typeName", "webhook")),
        team_id=raw.get("teamId"),
        url=raw.get("url"),
        raw=raw,
    )


def normalize_list(items: List[Dict[str, Any]], fn) -> List[Any]:
    return [fn(item) for item in items or []]


# ── NormalizedDocument (Shielva KB) ────────────────────────────────────────

def normalize_scenario_document(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Make scenario into a ``NormalizedDocument``.

    Tenant-scoped id: ``f"{tenant_id}_{source_id}"`` — never the bare Make id,
    so the same scenario id from two tenants never collides in the KB.
    """
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id", ""))
    name = str(raw.get("name", ""))
    description = str(
        raw.get("description") or raw.get("note") or ""
    )
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Scenario {source_id}",
        content=description,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=_parse_dt(raw.get("created") or raw.get("createdAt")),
        updated_at=_parse_dt(
            raw.get("updated") or raw.get("updatedAt") or raw.get("lastEdit")
        ),
        metadata={
            "team_id": raw.get("teamId"),
            "is_active": bool(raw.get("isActive")),
            "is_paused": bool(raw.get("isPaused")),
            "scheduling": raw.get("scheduling"),
            "kind": "make.scenario",
        },
    )
