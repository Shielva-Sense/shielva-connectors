"""Transforms raw Wufoo API responses into NormalizedDocument objects.

Wufoo entries are flat dicts: {EntryId, DateCreated, CreatedBy, Field1, Field2, ...}.
We normalize them so downstream Shielva search/ingest treats each entry as a doc.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)


def _parse_dt(value: str) -> Optional[datetime]:
    """Parse a Wufoo timestamp like '2024-01-01 12:00:00' to aware UTC datetime."""
    if not value:
        return None
    fmts = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")
    for fmt in fmts:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def normalize_entry(
    entry: Dict[str, Any],
    form_hash: str,
    connector_id: str,
    tenant_id: str,
):
    """Convert a Wufoo entry dict to a NormalizedDocument.

    Imported lazily so the module is importable without the SDK in tests.
    """
    from shared.base_connector import NormalizedDocument

    entry_id = str(entry.get("EntryId", ""))
    created = _parse_dt(entry.get("DateCreated", ""))
    updated = _parse_dt(entry.get("DateUpdated", "")) or created
    author = entry.get("CreatedBy", "") or ""

    # Title = first non-empty field value; content = "key=value" joined.
    field_values: List[str] = []
    title: str = ""
    for key, val in entry.items():
        if not key.startswith("Field"):
            continue
        if val in (None, ""):
            continue
        sval = str(val)
        if not title:
            title = sval[:120]
        field_values.append(f"{key}={sval}")
    content = "\n".join(field_values) or "(empty entry)"

    return NormalizedDocument(
        id=f"{connector_id}_{form_hash}_{entry_id}",
        source_id=entry_id,
        title=title or f"Entry {entry_id}",
        content=content,
        content_type="text",
        author=author or None,
        created_at=created,
        updated_at=updated,
        source="wufoo_connector",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "form_hash": form_hash,
            "entry_id": entry_id,
            "created_by": author,
            "raw": entry,
        },
    )
