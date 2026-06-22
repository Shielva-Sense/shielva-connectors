"""Iterable → Shielva NormalizedDocument adapters.

Single owner of every Iterable→Shielva shape transform. Keep this module
import-light: it MUST NOT import httpx or anything from `client/`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from helpers.utils import ms_to_dt


# ── Pass-through unwrappers ────────────────────────────────────────────────


def normalize_user(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten Iterable's nested `{"user": {...}}` envelope.

    `GET /users/getByEmail` returns `{"user": {...}}`; `GET /users/byUserId/{id}`
    returns the user object directly. This helper accepts either.
    """
    if isinstance(raw, dict) and isinstance(raw.get("user"), dict):
        return raw["user"]
    return raw if isinstance(raw, dict) else {}


def normalize_lists(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`GET /lists` returns `{"lists": [...]}` — return the inner array."""
    if not isinstance(raw, dict):
        return []
    lists = raw.get("lists")
    return lists if isinstance(lists, list) else []


def normalize_campaigns(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`GET /campaigns` returns `{"campaigns": [...]}` — return the inner array."""
    if not isinstance(raw, dict):
        return []
    campaigns = raw.get("campaigns")
    return campaigns if isinstance(campaigns, list) else []


def normalize_channels(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`GET /channels` returns `{"channels": [...]}` — return the inner array."""
    if not isinstance(raw, dict):
        return []
    channels = raw.get("channels")
    return channels if isinstance(channels, list) else []


def normalize_catalogs(raw: Dict[str, Any]) -> List[str]:
    """`GET /catalogs` legacy returns `{"params": {"catalogNames": [...]}}` and
    modern shape `{"catalogs": [...]}`. Return a flat list of catalog names.
    """
    if not isinstance(raw, dict):
        return []
    modern = raw.get("catalogs")
    if isinstance(modern, list):
        return [str(c.get("name") if isinstance(c, dict) else c) for c in modern]
    legacy = (raw.get("params") or {}).get("catalogNames")
    if isinstance(legacy, list):
        return [str(c) for c in legacy]
    return []


# ── NormalizedDocument adapters ────────────────────────────────────────────


def normalize_template(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn an Iterable template payload into a NormalizedDocument.

    Imports `NormalizedDocument` lazily so the helpers module is importable
    in environments where `shared.base_connector` is stubbed.
    """
    from shared.base_connector import NormalizedDocument

    template = raw or {}
    source_id = str(template.get("templateId") or template.get("id") or "")
    html = template.get("html")
    body = html if html else (template.get("plainText") or "")
    content_type = "html" if html else "text"

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=template.get("name") or f"Iterable template {source_id}",
        content=body,
        content_type=content_type,
        author=str(template.get("creatorUserId") or "") or None,
        created_at=ms_to_dt(template.get("createdAt")),
        updated_at=ms_to_dt(template.get("updatedAt")),
        source="iterable",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "templateId": template.get("templateId"),
            "messageMedium": template.get("messageMedium"),
            "templateType": template.get("templateType"),
            "campaignId": template.get("campaignId"),
            "kind": "iterable.template",
        },
    )


def normalize_list_as_document(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn an Iterable list (audience segment) into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    lst = raw or {}
    source_id = str(lst.get("id") or lst.get("listId") or "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=lst.get("name") or f"Iterable list {source_id}",
        content=lst.get("description") or "",
        content_type="text",
        author=str(lst.get("creatorUserId") or "") or None,
        created_at=ms_to_dt(lst.get("createdAt")),
        updated_at=ms_to_dt(lst.get("updatedAt")),
        source="iterable",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "listId": lst.get("id") or lst.get("listId"),
            "listType": lst.get("listType"),
            "kind": "iterable.list",
        },
    )
