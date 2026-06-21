"""Normalize Algolia API resources into ``NormalizedDocument``.

Algolia objects are arbitrary JSON dicts identified by ``objectID``. The
normalizer applies best-effort heuristics for ``title`` / ``content`` so
ingested documents have something useful for downstream search, while
preserving the full raw payload in ``metadata`` for lossless round-trip.

Single owner of all Algolia → NormalizedDocument projection.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp or epoch milliseconds; ``None`` on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Algolia ``lastBuildTimeS`` is seconds; older endpoints emit ms.
        try:
            if value > 10_000_000_000:  # > year 2286 in seconds → must be ms
                return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _heuristic_title(obj: Dict[str, Any]) -> str:
    """Pick a reasonable display title from common Algolia object fields."""
    for key in ("title", "name", "displayName", "headline", "label"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    object_id = obj.get("objectID")
    return str(object_id) if object_id else ""


def _heuristic_content(obj: Dict[str, Any]) -> str:
    """Pick a reasonable searchable content blob from common fields."""
    for key in ("description", "content", "body", "text", "summary"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Fall back to serialised JSON so the full record is searchable.
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(obj)


def normalize_object(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
    index_name: str,
):
    """Project a single Algolia object → ``NormalizedDocument``.

    ``id = f"{tenant_id}_{source_id}"`` — tenant-scoped, as required by the
    multi-tenant model.
    """
    from shared.base_connector import NormalizedDocument  # local import — avoid cycle

    source_id = str(raw.get("objectID", "") or "")
    created_at = _parse_dt(raw.get("createdAt") or raw.get("_createdAt"))
    updated_at = _parse_dt(
        raw.get("updatedAt") or raw.get("lastModified") or raw.get("_updatedAt")
    )

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=_heuristic_title(raw),
        content=_heuristic_content(raw),
        content_type="text",
        created_at=created_at,
        updated_at=updated_at,
        source=f"algolia.{index_name}",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "objectID": source_id,
            "index_name": index_name,
            "kind": "algolia.object",
            "raw": raw,
        },
    )


def normalize_index(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
):
    """Project one entry from ``GET /1/indexes`` → ``NormalizedDocument``.

    Used by ``sync()`` for inventory mode — exposes the list of indices as
    documents so the KB has searchable metadata about the application.
    """
    from shared.base_connector import NormalizedDocument

    name = str(raw.get("name", "") or "")
    entries = int(raw.get("entries", 0) or 0)
    updated_at = _parse_dt(raw.get("updatedAt") or raw.get("lastBuildTimeS"))

    return NormalizedDocument(
        id=f"{tenant_id}_{name}",
        source_id=name,
        title=name,
        content=f"Algolia index '{name}' — {entries} entries",
        content_type="text",
        updated_at=updated_at,
        source="algolia.index",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "entries": entries,
            "dataSize": raw.get("dataSize"),
            "fileSize": raw.get("fileSize"),
            "lastBuildTimeS": raw.get("lastBuildTimeS"),
            "primary": raw.get("primary"),
            "kind": "algolia.index",
        },
    )
