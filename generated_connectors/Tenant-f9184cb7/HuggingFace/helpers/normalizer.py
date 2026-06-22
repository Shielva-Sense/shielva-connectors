"""Normalize raw HuggingFace Hub responses into ``NormalizedDocument`` objects.

Used by ``sync()`` and ``get_model()`` callers that want a uniform document
shape ingestible by Shielva's knowledge-base pipeline.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            # HF returns "2024-01-15T12:34:56.000Z" — normalize Z → +00:00.
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def normalize_model(
    model: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Convert a HuggingFace Hub model object to a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    if not isinstance(model, dict):
        model = {}
    model_id = model.get("id") or model.get("modelId") or ""
    author = model.get("author") or (model_id.split("/", 1)[0] if "/" in model_id else "")
    description = (
        model.get("description")
        or (model.get("cardData", {}) or {}).get("description")
        or ""
    )
    tags = model.get("tags") or []
    downloads = model.get("downloads")
    likes = model.get("likes")
    pipeline_tag = model.get("pipeline_tag")
    created_at = _parse_dt(model.get("createdAt")) or datetime.now(timezone.utc)
    updated_at = _parse_dt(model.get("lastModified") or model.get("updatedAt"))

    return NormalizedDocument(
        id=f"{connector_id}_{model_id.replace('/', '__')}",
        source_id=model_id,
        title=model_id,
        content=description or model_id,
        content_type="text",
        source_url=f"https://huggingface.co/{model_id}" if model_id else None,
        author=author,
        created_at=created_at,
        updated_at=updated_at,
        source="huggingface",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "tags": tags,
            "downloads": downloads,
            "likes": likes,
            "pipeline_tag": pipeline_tag,
            "kind": "huggingface.model",
        },
    )
