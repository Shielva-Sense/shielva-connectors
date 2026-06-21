"""Normalize Qdrant API resources into NormalizedDocument."""
from datetime import datetime, timezone
from typing import Any, Dict


def normalize_collection(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    collection_name: str = "",
):
    """Turn a Qdrant collection description into a NormalizedDocument.

    Qdrant does not surface a creation timestamp on collections, so we stamp
    `utcnow()` on the doc. The interesting payload lives in `metadata` —
    vector size, distance metric, points_count, segment count, status.
    """
    from shared.base_connector import NormalizedDocument

    # `raw` is the body returned by `GET /collections/{name}` — the actual
    # collection payload sits under `result`. We also accept a flat shape so
    # callers that have already unwrapped don't need to wrap again.
    payload = raw.get("result", raw) if isinstance(raw, dict) else {}
    name = collection_name or payload.get("name", "") or payload.get("collection_name", "")

    config = payload.get("config", {}) or {}
    params = config.get("params", {}) or {}
    vectors = params.get("vectors", {}) or {}

    # Vector size / distance — Qdrant returns either a single vector spec or
    # a named-vector map. Surface both shapes verbatim in metadata.
    vector_size: Any = None
    distance: Any = None
    if isinstance(vectors, dict) and "size" in vectors:
        vector_size = vectors.get("size")
        distance = vectors.get("distance")

    summary_lines = [
        f"Collection: {name}",
        f"Status: {payload.get('status', '')}",
        f"Points: {payload.get('points_count', 0)}",
        f"Segments: {payload.get('segments_count', 0)}",
    ]
    if vector_size is not None:
        summary_lines.append(f"Vector size: {vector_size} ({distance})")
    content = "\n".join(summary_lines)

    return NormalizedDocument(
        id=f"{connector_id}_{name}",
        source_id=name,
        title=f"Qdrant collection: {name}",
        content=content,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata={
            "vectors": vectors,
            "vector_size": vector_size,
            "distance": distance,
            "points_count": payload.get("points_count"),
            "indexed_vectors_count": payload.get("indexed_vectors_count"),
            "segments_count": payload.get("segments_count"),
            "status": payload.get("status", ""),
            "optimizer_status": payload.get("optimizer_status"),
            "kind": "qdrant.collection",
        },
    )
