"""Shared utilities for the Document360 connector.

Pure helpers — no HTTP, no business logic. Keep this module dependency-light
so it can be imported from connector.py, helpers/normalizer.py, and tests.
"""
from typing import Any, Dict, Iterable, List, Optional

_PUBLIC_DOC_BASE = "https://apidocs.document360.com"


def build_article_url(
    article_id: str,
    project_slug: Optional[str] = None,
    language_code: str = "en",
) -> str:
    """Build a best-effort public URL for a Document360 article.

    Document360 does not return the canonical public URL in every API response,
    so we synthesize one from the slug and id. When no slug is known we still
    return a stable per-id URL — callers downstream (search-engine indexing)
    rely on `id` for dedupe so a synthesized URL is acceptable.
    """
    slug = (project_slug or "").strip("/")
    if slug:
        return f"https://{slug}.document360.io/{language_code}/article/{article_id}"
    return f"{_PUBLIC_DOC_BASE}/article/{article_id}"


def extract_id(obj: Dict[str, Any], *keys: str, default: str = "") -> str:
    """Return the first non-empty string value among *keys* from *obj*.

    Document360 responses use mixed casing (`id`, `Id`, `articleId`) — this
    helper makes downstream normalization tolerant without a per-key if-chain.
    """
    for key in keys:
        value = obj.get(key)
        if value:
            return str(value)
    return default


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Coerce *value* to int, returning *default* on any failure."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def envelope_items(payload: Any) -> List[Dict[str, Any]]:
    """Normalise Document360 list responses into a flat list of dicts.

    Some endpoints return a bare JSON array, others a `{items: [...]}` or
    `{result: {data: [...]}}` envelope. Callers should not have to care.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "articles", "categories", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = value.get("data") or value.get("items")
                if isinstance(nested, list):
                    return [x for x in nested if isinstance(x, dict)]
    return []


def chunked(iterable: Iterable[Any], size: int) -> Iterable[List[Any]]:
    """Yield successive *size*-length chunks from *iterable*."""
    batch: List[Any] = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
