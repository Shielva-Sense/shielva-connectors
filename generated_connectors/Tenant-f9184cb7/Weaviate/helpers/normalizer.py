"""Normalize Weaviate API resources into NormalizedDocument."""
import json
from typing import Any, Dict, Optional

from helpers.utils import parse_unix_ms


_TITLE_CANDIDATES = ("title", "name", "subject", "text", "content")


def _pick_title(properties: Dict[str, Any], fallback: str) -> str:
    """Return the first non-empty string-valued property that looks like a title."""
    if not isinstance(properties, dict):
        return fallback
    for key in _TITLE_CANDIDATES:
        val = properties.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:120]
    return fallback


def normalize_object(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    *,
    default_class: Optional[str] = None,
):
    """Turn a Weaviate object into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    if not isinstance(raw, dict):
        raw = {}

    source_id = str(raw.get("id") or "")
    class_name = raw.get("class") or default_class or ""
    properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    additional = raw.get("additional") if isinstance(raw.get("additional"), dict) else {}
    tenant = raw.get("tenant")

    fallback_title = f"{class_name}:{source_id}" if class_name and source_id else (source_id or "weaviate.object")
    title = _pick_title(properties, fallback_title)

    try:
        content = json.dumps(properties, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        content = str(properties)

    created_at = parse_unix_ms(raw.get("creationTimeUnix"))
    updated_at = parse_unix_ms(raw.get("lastUpdateTimeUnix") or raw.get("creationTimeUnix"))

    metadata: Dict[str, Any] = {
        "class": class_name,
        "vector_present": "vector" in raw and raw.get("vector") is not None,
        "tenant": tenant,
        "kind": "weaviate.object",
    }
    if additional:
        metadata["additional"] = additional

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}" if source_id else f"{tenant_id}_unknown",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        source=f"weaviate.{class_name}" if class_name else "weaviate",
        source_url=None,
        url=None,
        author=None,
        created_at=created_at,
        updated_at=updated_at,
        metadata=metadata,
    )
