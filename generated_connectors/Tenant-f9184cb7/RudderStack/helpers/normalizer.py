"""Normalize Rudderstack API resources into NormalizedDocument.

RudderStack is event-streaming, so there is no document corpus to sync.
For symmetry with other connectors (and for KB ingest of control-plane
inventory) we still expose normalizers for Sources + Destinations.
"""
from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_source(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Rudderstack control-plane source into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source = raw.get("source", raw) if isinstance(raw, dict) else {}
    source_id = source.get("id") or source.get("sourceId") or ""
    name = source.get("name", "")
    source_type = source.get("type", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Source {source_id}",
        content=f"{source_type} source" if source_type else (name or source_id),
        content_type="text",
        author=None,
        created_at=_parse_dt(source.get("createdAt") or source.get("created_at")),
        updated_at=_parse_dt(source.get("updatedAt") or source.get("updated_at")),
        metadata={
            "type": source_type,
            "enabled": bool(source.get("enabled", True)),
            "writeKey": source.get("writeKey", ""),
            "workspaceId": source.get("workspaceId", ""),
            "kind": "rudderstack.source",
        },
    )


def normalize_destination(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Rudderstack control-plane destination into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    dest = raw.get("destination", raw) if isinstance(raw, dict) else {}
    dest_id = dest.get("id") or dest.get("destinationId") or ""
    name = dest.get("name", "")
    dest_type = dest.get("type", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{dest_id}",
        source_id=dest_id,
        title=name or f"Destination {dest_id}",
        content=f"{dest_type} destination" if dest_type else (name or dest_id),
        content_type="text",
        author=None,
        created_at=_parse_dt(dest.get("createdAt") or dest.get("created_at")),
        updated_at=_parse_dt(dest.get("updatedAt") or dest.get("updated_at")),
        metadata={
            "type": dest_type,
            "enabled": bool(dest.get("enabled", True)),
            "sourceId": dest.get("sourceId", ""),
            "kind": "rudderstack.destination",
        },
    )
