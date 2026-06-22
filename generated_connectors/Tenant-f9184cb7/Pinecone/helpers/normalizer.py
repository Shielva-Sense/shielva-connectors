"""Normalize Pinecone control-plane resources into NormalizedDocument.

Pinecone is primarily a vector sink rather than a content source — the
"document" here is the index spec itself, which gives the KB a discoverable
audit trail of every index the tenant owns plus its vector counts.
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


def normalize_index(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    stats: Dict[str, Any] | None = None,
):
    """Turn a Pinecone index spec into a NormalizedDocument.

    `raw` shape (from describe_index):
        {name, dimension, metric, host, spec: {...}, status: {...}}

    `stats` (optional, from describe_index_stats):
        {dimension, totalVectorCount, namespaces: {...}, indexFullness}
    """
    from shared.base_connector import NormalizedDocument

    spec = raw if isinstance(raw, dict) else {}
    name = str(spec.get("name", ""))
    dimension = spec.get("dimension", 0)
    metric = spec.get("metric", "")
    serverless = (spec.get("spec", {}) or {}).get("serverless", {}) or {}
    pod = (spec.get("spec", {}) or {}).get("pod", {}) or {}

    stats = stats or {}
    total = int(stats.get("totalVectorCount", 0) or 0)
    fullness = stats.get("indexFullness")
    namespaces = stats.get("namespaces", {}) or {}

    summary_parts = [f"Pinecone index '{name}'", f"metric={metric}", f"dim={dimension}"]
    if total:
        summary_parts.append(f"vectors={total}")
    if fullness is not None:
        summary_parts.append(f"fullness={fullness}")
    content = ", ".join(summary_parts)

    return NormalizedDocument(
        id=f"{tenant_id}_{name}" if name else f"{tenant_id}_pinecone_unknown",
        source_id=name,
        title=name,
        content=content,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=_parse_dt(spec.get("createdAt") or spec.get("created_at")),
        updated_at=_parse_dt(spec.get("updatedAt") or spec.get("updated_at")),
        metadata={
            "kind": "pinecone.index",
            "dimension": dimension,
            "metric": metric,
            "host": spec.get("host", ""),
            "status": spec.get("status", {}) or {},
            "serverless": serverless,
            "pod": pod,
            "total_vector_count": total,
            "index_fullness": fullness,
            "namespaces": list(namespaces.keys()),
        },
    )
