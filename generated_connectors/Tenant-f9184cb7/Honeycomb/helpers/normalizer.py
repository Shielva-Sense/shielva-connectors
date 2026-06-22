"""Normalize Honeycomb API resources into `NormalizedDocument`.

Honeycomb stores observability events, not documents, so the meaningful
"document" surface for KB sync is the catalog of datasets + their columns,
saved queries, triggers, and markers — each gets a `NormalizedDocument`
keyed by `f"{tenant_id}_{source_id}"` to enforce multi-tenant isolation.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse a Honeycomb RFC-3339 timestamp into an aware datetime.

    Returns None when the input is empty / unparseable so callers can decide
    whether to fall back to `datetime.now(tz=UTC)`.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _doc_id(tenant_id: str, source_id: str) -> str:
    """Multi-tenant NormalizedDocument id contract."""
    return f"{tenant_id}_{source_id}"


def normalize_dataset(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    columns: Optional[List[Dict[str, Any]]] = None,
):
    """Honeycomb dataset → NormalizedDocument.

    `columns` (when supplied by the caller) is folded into the content body
    so a tenant's vector store can answer "what fields does dataset X expose?"
    questions without a follow-up API call.
    """
    from shared.base_connector import NormalizedDocument

    slug = raw.get("slug") or raw.get("name") or ""
    name = raw.get("name") or slug
    description = raw.get("description") or ""
    content_lines = [
        f"Dataset: {name}",
        f"Slug: {slug}",
        f"Description: {description or '(none)'}",
        f"Last written: {raw.get('last_written_at', '') or '(unknown)'}",
    ]
    if columns:
        content_lines.append("")
        content_lines.append("Columns:")
        for col in columns:
            line = f"  - {col.get('key_name', '')} ({col.get('type', '?')})"
            if col.get("description"):
                line += f": {col['description']}"
            content_lines.append(line)
    created = _parse_dt(raw.get("created_at")) or datetime.now(timezone.utc)
    updated = _parse_dt(raw.get("last_written_at")) or created
    return NormalizedDocument(
        id=_doc_id(tenant_id, slug),
        source_id=slug,
        title=name,
        content="\n".join(content_lines),
        content_type="text",
        source_url=f"https://ui.honeycomb.io/datasets/{slug}",
        url=f"https://ui.honeycomb.io/datasets/{slug}",
        author="honeycomb",
        created_at=created,
        updated_at=updated,
        metadata={
            "kind": "honeycomb.dataset",
            "slug": slug,
            "regular_columns_count": int(raw.get("regular_columns_count", 0) or 0),
            "expand_json_depth": int(raw.get("expand_json_depth", 0) or 0),
            "column_count": len(columns or []),
        },
    )


def normalize_column(
    raw: Dict[str, Any],
    dataset_slug: str,
    connector_id: str,
    tenant_id: str,
):
    """Honeycomb column → NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    key_name = raw.get("key_name") or raw.get("id") or ""
    source_id = f"{dataset_slug}:{key_name}"
    return NormalizedDocument(
        id=_doc_id(tenant_id, source_id),
        source_id=source_id,
        title=f"{dataset_slug}.{key_name}",
        content=str(raw.get("description") or ""),
        content_type="text",
        author="honeycomb",
        created_at=_parse_dt(raw.get("last_written")) or datetime.now(timezone.utc),
        updated_at=_parse_dt(raw.get("last_written")) or datetime.now(timezone.utc),
        metadata={
            "kind": "honeycomb.column",
            "dataset": dataset_slug,
            "key_name": key_name,
            "type": raw.get("type", "string"),
            "hidden": bool(raw.get("hidden", False)),
        },
    )


def normalize_trigger(
    raw: Dict[str, Any],
    dataset_slug: str,
    connector_id: str,
    tenant_id: str,
):
    """Honeycomb trigger → NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    trigger_id = str(raw.get("id") or raw.get("name") or "")
    source_id = f"{dataset_slug}:trigger:{trigger_id}"
    return NormalizedDocument(
        id=_doc_id(tenant_id, source_id),
        source_id=source_id,
        title=raw.get("name") or trigger_id,
        content=(
            f"Trigger {raw.get('name', '')} on dataset {dataset_slug}.\n"
            f"Threshold: {raw.get('threshold', {})}\n"
            f"Frequency: {raw.get('frequency', 0)}s\n"
            f"Alert type: {raw.get('alert_type', '')}"
        ),
        content_type="text",
        author="honeycomb",
        created_at=_parse_dt(raw.get("created_at")) or datetime.now(timezone.utc),
        updated_at=_parse_dt(raw.get("updated_at") or raw.get("created_at"))
        or datetime.now(timezone.utc),
        metadata={
            "kind": "honeycomb.trigger",
            "dataset": dataset_slug,
            "trigger_id": trigger_id,
            "query_id": raw.get("query_id", ""),
            "threshold": raw.get("threshold", {}),
            "frequency": raw.get("frequency", 0),
            "alert_type": raw.get("alert_type", ""),
        },
    )


def normalize_marker(
    raw: Dict[str, Any],
    dataset_slug: str,
    connector_id: str,
    tenant_id: str,
):
    """Honeycomb marker → NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    marker_id = str(raw.get("id") or raw.get("message") or "")
    source_id = f"{dataset_slug}:marker:{marker_id}"
    start = raw.get("start_time")
    return NormalizedDocument(
        id=_doc_id(tenant_id, source_id),
        source_id=source_id,
        title=raw.get("message") or marker_id,
        content=f"Marker on {dataset_slug}: {raw.get('message', '')}",
        content_type="text",
        url=raw.get("url"),
        source_url=raw.get("url"),
        author="honeycomb",
        created_at=(
            datetime.fromtimestamp(int(start), tz=timezone.utc)
            if isinstance(start, (int, float))
            else datetime.now(timezone.utc)
        ),
        updated_at=datetime.now(timezone.utc),
        metadata={
            "kind": "honeycomb.marker",
            "dataset": dataset_slug,
            "marker_id": marker_id,
            "type": raw.get("type", "deploy"),
            "start_time": raw.get("start_time"),
            "end_time": raw.get("end_time"),
        },
    )


def normalize_query_result(
    raw: Dict[str, Any],
    dataset_slug: str,
    connector_id: str,
    tenant_id: str,
):
    """Honeycomb query-result envelope → NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    rid = str(raw.get("id") or "")
    source_id = f"{dataset_slug}:queryresult:{rid}"
    data = raw.get("data") or {}
    return NormalizedDocument(
        id=_doc_id(tenant_id, source_id),
        source_id=source_id,
        title=f"Query result {rid}",
        content=str(data),
        content_type="text",
        author="honeycomb",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata={
            "kind": "honeycomb.query_result",
            "dataset": dataset_slug,
            "result_id": rid,
            "complete": bool(raw.get("complete", False)),
        },
    )
