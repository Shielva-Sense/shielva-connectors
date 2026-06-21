"""Normalize Crisp API resources into NormalizedDocument.

Crisp returns most responses inside a top-level `{"data": ...}` envelope; the
normalizers accept either the envelope or the inner payload so they can be
invoked on a list iteration straight from the API.
"""
from typing import Any, Dict, Optional

from shared.base_connector import NormalizedDocument

from helpers.utils import safe_get, ts_to_dt


def _unwrap(raw: Dict[str, Any]) -> Dict[str, Any]:
    """If the raw payload is the API envelope `{"data": {...}}`, return the inner."""
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        return raw["data"]
    return raw or {}


def normalize_conversation(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Turn a Crisp conversation object into a NormalizedDocument."""
    data = _unwrap(raw)
    session_id = data.get("session_id") or data.get("id") or ""
    meta = data.get("meta") or {}
    subject = meta.get("subject") or data.get("preview") or session_id or "Crisp conversation"
    nickname = meta.get("nickname") or ""
    email = meta.get("email") or ""
    snippet = data.get("last_message") or data.get("preview") or ""

    doc_id = f"{tenant_id}_{session_id}" if session_id else f"{tenant_id}_{connector_id}"

    return NormalizedDocument(
        id=doc_id,
        source_id=session_id,
        title=str(subject),
        content=str(snippet),
        content_type="text",
        author=email or nickname or None,
        created_at=ts_to_dt(data.get("created_at")),
        updated_at=ts_to_dt(data.get("updated_at")),
        metadata={
            "tenant_id": tenant_id,
            "kind": "crisp.conversation",
            "state": data.get("state"),
            "website_id": data.get("website_id"),
            "assigned": data.get("assigned"),
            "segments": data.get("segments", []),
        },
    )


def normalize_person(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Turn a Crisp people-profile object into a NormalizedDocument."""
    data = _unwrap(raw)
    people_id = data.get("people_id") or data.get("id") or ""
    person = data.get("person") or {}
    nickname = person.get("nickname") or ""
    email = data.get("email") or person.get("email") or ""
    segments = data.get("segments") or []
    title = nickname or email or people_id or "Crisp contact"
    content_parts = [str(p) for p in (nickname, email, ", ".join(map(str, segments))) if p]
    content = " · ".join(content_parts)

    doc_id = f"{tenant_id}_{people_id}" if people_id else f"{tenant_id}_{connector_id}"

    return NormalizedDocument(
        id=doc_id,
        source_id=people_id,
        title=title,
        content=content,
        content_type="text",
        author=email or nickname or None,
        created_at=ts_to_dt(data.get("created_at")),
        updated_at=ts_to_dt(data.get("updated_at")),
        metadata={
            "tenant_id": tenant_id,
            "kind": "crisp.person",
            "email": email,
            "segments": segments,
            "person": person,
        },
    )


def normalize_helpdesk_article(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Turn a Crisp helpdesk article into a NormalizedDocument."""
    data = _unwrap(raw)
    article_id = data.get("article_id") or data.get("id") or ""
    title = data.get("title") or article_id or "Crisp article"
    content = data.get("content") or data.get("description") or ""

    doc_id = f"{tenant_id}_{article_id}" if article_id else f"{tenant_id}_{connector_id}"

    return NormalizedDocument(
        id=doc_id,
        source_id=article_id,
        title=str(title),
        content=str(content),
        content_type=data.get("content_type", "text"),
        source_url=data.get("url"),
        author=safe_get(data, "author"),
        created_at=ts_to_dt(data.get("created_at")),
        updated_at=ts_to_dt(data.get("updated_at")),
        metadata={
            "tenant_id": tenant_id,
            "kind": "crisp.helpdesk",
            "locale": data.get("locale"),
            "category_id": data.get("category_id"),
            "visibility": data.get("visibility"),
        },
    )
