"""Normalize PlanetScale API resources into NormalizedDocument.

Tenant-scoped id: ``f"{tenant_id}_{source_id}"`` so the same database surfaced
to two tenants stays disjoint in the knowledge base. The source string is
prefixed with ``planetscale.`` so KB consumers can filter by provider surface.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.base_connector import NormalizedDocument


def _parse_dt(value: Any) -> Optional[datetime]:
    """Coerce an RFC 3339 / ISO string into a tz-aware datetime, else None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _region_slug(region_obj: Any) -> str:
    """Pull a region slug out of either a dict or a bare string."""
    if isinstance(region_obj, dict):
        return str(region_obj.get("slug") or region_obj.get("name") or "")
    return str(region_obj or "")


def normalize_database(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Project a PlanetScale database object into a NormalizedDocument."""
    db_id = str(raw.get("id") or raw.get("name") or "")
    name = str(raw.get("name", db_id))
    plan = str(raw.get("plan", ""))
    region = _region_slug(raw.get("region"))
    state = str(raw.get("state", ""))
    org = raw.get("organization") or ""

    content_lines = [
        f"PlanetScale database: {name}",
        f"Plan: {plan or '(unspecified)'}",
        f"Region: {region or '(unspecified)'}",
        f"State: {state or '(unknown)'}",
    ]
    if isinstance(org, str) and org:
        content_lines.append(f"Organization: {org}")
    content = "\n".join(content_lines)

    return NormalizedDocument(
        id=f"{tenant_id}_{db_id}",
        source_id=db_id,
        title=name,
        content=content,
        content_type="text",
        source="planetscale.databases",
        source_url=str(raw.get("html_url", "")) or None,
        url=str(raw.get("html_url", "")) or None,
        author=str(org) if isinstance(org, str) else None,
        created_at=_parse_dt(raw.get("created_at")) or _now(),
        updated_at=_parse_dt(raw.get("updated_at")) or _now(),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "plan": plan,
            "region": region,
            "state": state,
            "kind": "planetscale.database",
        },
    )


def normalize_branch(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Project a PlanetScale branch object into a NormalizedDocument."""
    branch_id = str(raw.get("id") or raw.get("name") or "")
    name = str(raw.get("name", branch_id))
    parent = str(raw.get("parent_branch", ""))
    production = bool(raw.get("production", False))
    ready = bool(raw.get("ready", False))

    content = "\n".join(
        [
            f"PlanetScale branch: {name}",
            f"Parent branch: {parent or '(none)'}",
            f"Production: {'yes' if production else 'no'}",
            f"Ready: {'yes' if ready else 'no'}",
        ]
    )

    return NormalizedDocument(
        id=f"{tenant_id}_{branch_id}",
        source_id=branch_id,
        title=name,
        content=content,
        content_type="text",
        source="planetscale.branches",
        source_url=str(raw.get("html_url", "")) or None,
        url=str(raw.get("html_url", "")) or None,
        author=parent or None,
        created_at=_parse_dt(raw.get("created_at")) or _now(),
        updated_at=_parse_dt(raw.get("updated_at")) or _now(),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "parent_branch": parent,
            "production": production,
            "ready": ready,
            "kind": "planetscale.branch",
        },
    )
