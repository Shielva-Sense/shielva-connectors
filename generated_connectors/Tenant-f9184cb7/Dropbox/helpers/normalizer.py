"""Normalize Dropbox API resources into ``NormalizedDocument``.

Owns all `raw API response → NormalizedDocument` transformations. No HTTP,
no retry, no state. The ``id`` convention is project-wide: ``f"{tenant_id}_{source_id}"``.
"""
from __future__ import annotations

from typing import Any, Dict

from helpers.utils import parse_dt, utcnow


def _source_id_for(entry: Dict[str, Any]) -> str:
    """Stable Dropbox source id — prefer ``entry.id`` (survives rename/move).

    Falls back to ``path_lower`` for entries that never get an id assigned
    (e.g. some legacy folder responses).
    """
    return entry.get("id") or entry.get("path_lower") or entry.get("name") or ""


def _dropbox_url_for(path_display: str) -> str:
    if not path_display:
        return ""
    if path_display.startswith("/"):
        return f"https://www.dropbox.com/home{path_display}"
    return f"https://www.dropbox.com/home/{path_display}"


def normalize_file(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Dropbox ``.tag == 'file'`` entry into a ``NormalizedDocument``.

    Project rule: ``NormalizedDocument.id = f"{tenant_id}_{source_id}"``.
    """
    from shared.base_connector import NormalizedDocument

    source_id = _source_id_for(raw)
    name = raw.get("name", "")
    path_display = raw.get("path_display", "") or ""
    path_lower = raw.get("path_lower", "") or ""
    size = raw.get("size", 0)
    rev = raw.get("rev", "")
    client_modified = raw.get("client_modified")
    server_modified = raw.get("server_modified")

    content_parts = [
        f"Name: {name}",
        f"Path: {path_display}",
        "Type: file",
    ]
    if size:
        content_parts.append(f"Size: {size} bytes")
    if server_modified:
        content_parts.append(f"Server modified: {server_modified}")
    if client_modified:
        content_parts.append(f"Client modified: {client_modified}")
    if rev:
        content_parts.append(f"Revision: {rev}")

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}" if tenant_id else source_id,
        source_id=source_id,
        title=name or path_display or source_id,
        content="\n".join(content_parts),
        content_type="text",
        source_url=_dropbox_url_for(path_display),
        url=_dropbox_url_for(path_display),
        author=None,
        created_at=parse_dt(client_modified) or utcnow(),
        updated_at=parse_dt(server_modified) or utcnow(),
        source="dropbox",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "dropbox.file",
            "name": name,
            "path_display": path_display,
            "path_lower": path_lower,
            "size": size,
            "rev": rev,
            "is_downloadable": raw.get("is_downloadable", True),
            "content_hash": raw.get("content_hash", ""),
            "server_modified": server_modified,
            "client_modified": client_modified,
            "tag": "file",
        },
    )


def normalize_folder(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Dropbox ``.tag == 'folder'`` entry into a ``NormalizedDocument``."""
    from shared.base_connector import NormalizedDocument

    source_id = _source_id_for(raw)
    name = raw.get("name", "")
    path_display = raw.get("path_display", "") or ""
    path_lower = raw.get("path_lower", "") or ""

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}" if tenant_id else source_id,
        source_id=source_id,
        title=name or path_display or source_id,
        content=f"Folder: {path_display or name}",
        content_type="text",
        source_url=_dropbox_url_for(path_display),
        url=_dropbox_url_for(path_display),
        author=None,
        created_at=utcnow(),
        updated_at=utcnow(),
        source="dropbox",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "dropbox.folder",
            "name": name,
            "path_display": path_display,
            "path_lower": path_lower,
            "tag": "folder",
        },
    )


def normalize_entry(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Dispatch on the Dropbox ``.tag`` field."""
    tag = (raw or {}).get(".tag", "file")
    if tag == "folder":
        return normalize_folder(raw, connector_id, tenant_id)
    return normalize_file(raw, connector_id, tenant_id)
