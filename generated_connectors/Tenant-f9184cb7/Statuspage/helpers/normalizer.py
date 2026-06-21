"""Normalize Statuspage API resources into ``NormalizedDocument``.

Statuspage is an outbound incident-communications API, but ingesting
incidents + maintenances into the Shielva KB lets agents search "what was
the last DB outage" or "are there any scheduled maintenance windows next
week" without hitting the provider on every question.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return None


def _incident_body(raw: Dict[str, Any]) -> str:
    """Concat name + latest update body for searchability."""
    name = raw.get("name") or ""
    updates = raw.get("incident_updates") or []
    bodies = [u.get("body", "") for u in updates if isinstance(u, dict)]
    body = "\n\n".join(b for b in bodies if b)
    if name and body:
        return f"{name}\n\n{body}"
    return name or body or ""


def normalize_incident(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Statuspage incident → ``NormalizedDocument``.

    ``id`` is tenant-scoped (``f"{tenant_id}_{source_id}"``) so multi-tenant
    isolation is preserved at the KB layer.
    """
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id", ""))
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=raw.get("name", "") or f"Incident {source_id}",
        content=_incident_body(raw),
        content_type="text",
        source_url=raw.get("shortlink"),
        url=raw.get("shortlink"),
        author=None,
        created_at=_parse_dt(raw.get("created_at")),
        updated_at=_parse_dt(raw.get("updated_at")),
        metadata={
            "status": raw.get("status"),
            "impact": raw.get("impact"),
            "impact_override": raw.get("impact_override"),
            "page_id": raw.get("page_id"),
            "resolved_at": raw.get("resolved_at"),
            "monitoring_at": raw.get("monitoring_at"),
            "component_ids": [
                c.get("id") for c in raw.get("components") or [] if isinstance(c, dict)
            ],
            "kind": "statuspage.incident",
        },
        source="statuspage",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_maintenance(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Scheduled maintenance → ``NormalizedDocument``."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id", ""))
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=raw.get("name", "") or f"Maintenance {source_id}",
        content=_incident_body(raw),
        content_type="text",
        source_url=raw.get("shortlink"),
        url=raw.get("shortlink"),
        author=None,
        created_at=_parse_dt(raw.get("created_at")),
        updated_at=_parse_dt(raw.get("updated_at")),
        metadata={
            "status": raw.get("status"),
            "impact": raw.get("impact"),
            "scheduled_for": raw.get("scheduled_for"),
            "scheduled_until": raw.get("scheduled_until"),
            "page_id": raw.get("page_id"),
            "kind": "statuspage.maintenance",
        },
        source="statuspage",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
