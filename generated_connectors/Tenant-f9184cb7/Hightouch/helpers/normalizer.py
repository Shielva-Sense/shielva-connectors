"""Normalize Hightouch API resources into NormalizedDocument.

Hightouch is reverse-ETL — there is no document corpus to backfill. For
symmetry with other connectors (and for KB ingest of inventory) we expose
normalizers for Sources, Models, Destinations, and Syncs so the KB can
mirror what is configured in Hightouch.
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


def _id_str(raw: Dict[str, Any]) -> str:
    """Hightouch ids come as ints. Coerce for stable NormalizedDocument ids."""
    v = raw.get("id") or raw.get("Id") or ""
    return str(v) if v is not None else ""


def normalize_source(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Hightouch source into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source = raw if isinstance(raw, dict) else {}
    source_id = _id_str(source)
    name = source.get("name", "")
    source_type = source.get("type", "")
    slug = source.get("slug", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or slug or f"Source {source_id}",
        content=f"{source_type} source" if source_type else (name or source_id),
        content_type="text",
        author=None,
        created_at=_parse_dt(source.get("createdAt") or source.get("created_at")),
        updated_at=_parse_dt(source.get("updatedAt") or source.get("updated_at")),
        metadata={
            "type": source_type,
            "slug": slug,
            "kind": "hightouch.source",
        },
    )


def normalize_model(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Hightouch model into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    model = raw if isinstance(raw, dict) else {}
    model_id = _id_str(model)
    name = model.get("name", "")
    slug = model.get("slug", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{model_id}",
        source_id=model_id,
        title=name or slug or f"Model {model_id}",
        content=name or slug or model_id,
        content_type="text",
        author=None,
        created_at=_parse_dt(model.get("createdAt") or model.get("created_at")),
        updated_at=_parse_dt(model.get("updatedAt") or model.get("updated_at")),
        metadata={
            "slug": slug,
            "sourceId": model.get("sourceId"),
            "primaryKey": model.get("primaryKey"),
            "kind": "hightouch.model",
        },
    )


def normalize_destination(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Hightouch destination into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    dest = raw if isinstance(raw, dict) else {}
    dest_id = _id_str(dest)
    name = dest.get("name", "")
    dest_type = dest.get("type", "")
    slug = dest.get("slug", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{dest_id}",
        source_id=dest_id,
        title=name or slug or f"Destination {dest_id}",
        content=f"{dest_type} destination" if dest_type else (name or dest_id),
        content_type="text",
        author=None,
        created_at=_parse_dt(dest.get("createdAt") or dest.get("created_at")),
        updated_at=_parse_dt(dest.get("updatedAt") or dest.get("updated_at")),
        metadata={
            "type": dest_type,
            "slug": slug,
            "kind": "hightouch.destination",
        },
    )


def normalize_sync(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Hightouch sync into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    sync = raw if isinstance(raw, dict) else {}
    sync_id = _id_str(sync)
    slug = sync.get("slug") or f"sync-{sync_id}"
    return NormalizedDocument(
        id=f"{tenant_id}_{sync_id}",
        source_id=sync_id,
        title=sync.get("name") or slug,
        content=slug,
        content_type="text",
        author=None,
        created_at=_parse_dt(sync.get("createdAt") or sync.get("created_at")),
        updated_at=_parse_dt(sync.get("updatedAt") or sync.get("updated_at")),
        metadata={
            "slug": slug,
            "modelId": sync.get("modelId"),
            "destinationId": sync.get("destinationId"),
            "disabled": bool(sync.get("disabled", False)),
            "schedule": sync.get("schedule"),
            "kind": "hightouch.sync",
        },
    )
