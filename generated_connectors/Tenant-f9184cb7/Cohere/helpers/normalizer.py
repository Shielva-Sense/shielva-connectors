"""Normalize Cohere API resources into NormalizedDocument.

Cohere is an inference API — there is no native document corpus. These
normalisers exist so callers that want to land Cohere's *model catalogue* or
*fine-tuning datasets* in a Shielva knowledge base alongside other connectors
can do so with consistent shape.
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


def normalize_model(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Cohere model record into a NormalizedDocument.

    Tenant-scoped id = `f"{tenant_id}_{model['name']}"`.
    """
    from shared.base_connector import NormalizedDocument

    model = raw if isinstance(raw, dict) else {}
    source_id = model.get("name", "") or model.get("id", "")
    endpoints = model.get("endpoints", []) or []
    finetuned = bool(model.get("finetuned", False))
    content_parts = [source_id]
    if endpoints:
        content_parts.append("endpoints: " + ", ".join(endpoints))
    if finetuned:
        content_parts.append("(finetuned)")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=source_id,
        content=" ".join(content_parts).strip(),
        content_type="text",
        source="cohere.models",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "endpoints": endpoints,
            "finetuned": finetuned,
            "context_length": model.get("context_length"),
            "default_endpoints": model.get("default_endpoints", []) or [],
            "kind": "cohere.model",
        },
    )


def normalize_dataset(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Cohere fine-tune dataset record into a NormalizedDocument.

    Tenant-scoped id = `f"{tenant_id}_{dataset['id']}"`.
    """
    from shared.base_connector import NormalizedDocument

    ds = raw if isinstance(raw, dict) else {}
    source_id = ds.get("id", "") or ds.get("dataset_id", "")
    name = ds.get("name", "") or source_id
    dataset_type = ds.get("dataset_type", "")
    size = ds.get("size", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=f"{dataset_type} {size}".strip(),
        content_type="text",
        source="cohere.datasets",
        tenant_id=tenant_id,
        connector_id=connector_id,
        created_at=_parse_dt(ds.get("created_at")),
        updated_at=_parse_dt(ds.get("updated_at") or ds.get("created_at")),
        metadata={
            "dataset_type": dataset_type,
            "validation_status": ds.get("validation_status", ""),
            "size": size,
            "kind": "cohere.dataset",
        },
    )
