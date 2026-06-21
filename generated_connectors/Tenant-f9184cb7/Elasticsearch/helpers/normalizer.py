"""Normalize Elasticsearch API resources into NormalizedDocument.

Elasticsearch is a *destination* for the broader Shielva KB — most content
flows in via `index_document()` / `bulk()` from other connectors. The one
shape we normalise on the way back out is the **index inventory**: each
`/_cat/indices` row becomes a NormalizedDocument describing health, doc
count, and storage size for that index, so the tenant sees their clusters
modeled in the KB just like Drive folders or Notion pages.
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


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalize_index(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn one `/_cat/indices?format=json` row into a NormalizedDocument.

    Wire shape (Elasticsearch dotted-key columns):
        {
          "health": "green",
          "status": "open",
          "index": "products",
          "uuid": "abc...",
          "pri": "1", "rep": "1",
          "docs.count": "42", "docs.deleted": "0",
          "store.size": "12.3kb", "pri.store.size": "12.3kb"
        }
    """
    from shared.base_connector import NormalizedDocument

    if not isinstance(raw, dict):
        raw = {}

    index_name = raw.get("index", "") or ""
    source_id = index_name
    health = raw.get("health", "") or ""
    status = raw.get("status", "") or ""
    docs_count = _as_int(raw.get("docs.count"))
    docs_deleted = _as_int(raw.get("docs.deleted"))
    store_size = raw.get("store.size", "") or ""

    content = (
        f"Index: {index_name}\n"
        f"Health: {health}\n"
        f"Status: {status}\n"
        f"Documents: {docs_count} (deleted: {docs_deleted})\n"
        f"Store size: {store_size}"
    )

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=index_name or "(unnamed index)",
        content=content,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=_parse_dt(raw.get("creation_date")),
        updated_at=_parse_dt(raw.get("creation_date")),
        metadata={
            "health": health,
            "status": status,
            "uuid": raw.get("uuid", ""),
            "pri": _as_int(raw.get("pri")),
            "rep": _as_int(raw.get("rep")),
            "docs_count": docs_count,
            "docs_deleted": docs_deleted,
            "store_size": store_size,
            "kind": "elasticsearch.index",
        },
        source="elasticsearch",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
