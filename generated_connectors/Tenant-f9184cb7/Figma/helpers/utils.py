"""Figma connector — normalization and retry utilities."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, List, Optional

from models import ConnectorDocument


# ── stable id helpers ──────────────────────────────────────────────────────────

def _stable_id(prefix: str, key: str) -> str:
    """Produce a 16-char hex stable ID: sha256('<prefix>:<key>')[:16]."""
    raw = f"{prefix}:{key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── normalizers ───────────────────────────────────────────────────────────────

def normalize_file(raw: Dict[str, Any], project_id: str = "") -> ConnectorDocument:
    """Normalize a Figma file list entry into a ConnectorDocument.

    Stable id = sha256('file:<key>')[:16].
    ``project_id`` is the project that owns this file (optional but recommended).
    """
    key: str = raw.get("key", "")
    name: str = raw.get("name", "Untitled File")
    last_modified: str = raw.get("last_modified", "")
    thumbnail_url: str = raw.get("thumbnail_url", "")
    version: str = raw.get("version", "")

    stable = _stable_id("file", key)

    content_parts: List[str] = [f"File: {name}"]
    if key:
        content_parts.append(f"Key: {key}")
    if project_id:
        content_parts.append(f"Project ID: {project_id}")
    if last_modified:
        content_parts.append(f"Last modified: {last_modified}")
    if version:
        content_parts.append(f"Version: {version}")
    if thumbnail_url:
        content_parts.append(f"Thumbnail: {thumbnail_url}")

    metadata: Dict[str, Any] = {
        "file_key": key,
        "name": name,
        "project_id": project_id,
        "last_modified": last_modified,
        "thumbnail_url": thumbnail_url,
        "version": version,
        "object_type": "file",
        "source": "figma",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="design_file",
        metadata=metadata,
    )


def normalize_project(raw: Dict[str, Any], team_id: str = "") -> ConnectorDocument:
    """Normalize a Figma project object into a ConnectorDocument.

    Stable id = sha256('project:<id>')[:16].
    """
    project_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "Untitled Project")

    stable = _stable_id("project", project_id)

    content_parts: List[str] = [f"Project: {name}"]
    if project_id:
        content_parts.append(f"ID: {project_id}")
    if team_id:
        content_parts.append(f"Team ID: {team_id}")

    metadata: Dict[str, Any] = {
        "project_id": project_id,
        "name": name,
        "team_id": team_id,
        "object_type": "project",
        "source": "figma",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="figma_project",
        metadata=metadata,
    )


def normalize_component(raw: Dict[str, Any], team_id: str = "") -> ConnectorDocument:
    """Normalize a Figma component object into a ConnectorDocument.

    Stable id = sha256('component:<key>')[:16].
    ``team_id`` is the team that published this component (optional).
    """
    # Components from /teams/{id}/components are nested under meta.components[]
    # Each entry has: key, name, description, file_key, node_id, created_at, updated_at
    key: str = raw.get("key", "")
    name: str = raw.get("name", "Untitled Component")
    description: str = raw.get("description", "")
    file_key: str = raw.get("file_key", "")
    node_id: str = raw.get("node_id", "")
    created_at: str = raw.get("created_at", "")
    updated_at: str = raw.get("updated_at", "")

    stable = _stable_id("component", key)

    content_parts: List[str] = [f"Component: {name}"]
    if key:
        content_parts.append(f"Key: {key}")
    if team_id:
        content_parts.append(f"Team ID: {team_id}")
    if description:
        content_parts.append(f"Description: {description}")
    if file_key:
        content_parts.append(f"File key: {file_key}")
    if node_id:
        content_parts.append(f"Node ID: {node_id}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    metadata: Dict[str, Any] = {
        "component_key": key,
        "name": name,
        "team_id": team_id,
        "description": description,
        "file_key": file_key,
        "node_id": node_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "object_type": "component",
        "source": "figma",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="component",
        metadata=metadata,
    )


def normalize_comment(raw: Dict[str, Any], file_key: str = "") -> ConnectorDocument:
    """Normalize a Figma comment object into a ConnectorDocument.

    Stable id = sha256('comment:<id>')[:16].
    """
    comment_id: str = str(raw.get("id", ""))
    message: str = raw.get("message", "")
    created_at: str = raw.get("created_at", "")
    resolved_at: Optional[str] = raw.get("resolved_at")

    # User info is nested under the "user" key
    user_info: Dict[str, Any] = raw.get("user", {})
    user_handle: str = user_info.get("handle", "")

    stable = _stable_id("comment", comment_id)

    content_parts: List[str] = [f"Comment: {message}"]
    if comment_id:
        content_parts.append(f"ID: {comment_id}")
    if file_key:
        content_parts.append(f"File key: {file_key}")
    if user_handle:
        content_parts.append(f"Author: {user_handle}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if resolved_at:
        content_parts.append(f"Resolved: {resolved_at}")

    metadata: Dict[str, Any] = {
        "comment_id": comment_id,
        "message": message,
        "file_key": file_key,
        "user_handle": user_handle,
        "created_at": created_at,
        "resolved_at": resolved_at,
        "object_type": "comment",
        "source": "figma",
    }

    return ConnectorDocument(
        id=stable,
        title=f"Comment by {user_handle}" if user_handle else f"Comment {comment_id}",
        content="\n".join(content_parts),
        type="figma_comment",
        metadata=metadata,
    )


def normalize_style(raw: Dict[str, Any], team_id: str = "") -> ConnectorDocument:
    """Normalize a Figma published style into a ConnectorDocument.

    Stable id = sha256('style:<key>')[:16].
    ``team_id`` is the team that published this style (optional).
    """
    key: str = raw.get("key", "")
    name: str = raw.get("name", "Untitled Style")
    description: str = raw.get("description", "")
    style_type: str = raw.get("style_type", "")
    file_key: str = raw.get("file_key", "")
    node_id: str = raw.get("node_id", "")
    created_at: str = raw.get("created_at", "")
    updated_at: str = raw.get("updated_at", "")

    stable = _stable_id("style", key)

    content_parts: List[str] = [f"Style: {name}"]
    if key:
        content_parts.append(f"Key: {key}")
    if team_id:
        content_parts.append(f"Team ID: {team_id}")
    if style_type:
        content_parts.append(f"Type: {style_type}")
    if description:
        content_parts.append(f"Description: {description}")
    if file_key:
        content_parts.append(f"File key: {file_key}")
    if node_id:
        content_parts.append(f"Node ID: {node_id}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    metadata: Dict[str, Any] = {
        "style_key": key,
        "name": name,
        "team_id": team_id,
        "style_type": style_type,
        "description": description,
        "file_key": file_key,
        "node_id": node_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "object_type": "style",
        "source": "figma",
    }

    return ConnectorDocument(
        id=stable,
        title=name,
        content="\n".join(content_parts),
        type="figma_style",
        metadata=metadata,
    )


def normalize_version(raw: Dict[str, Any], file_key: str = "") -> ConnectorDocument:
    """Normalize a Figma file version entry into a ConnectorDocument.

    Stable id = sha256('version:<id>')[:16].
    """
    version_id: str = str(raw.get("id", ""))
    label: str = raw.get("label", "") or f"Version {version_id}"
    description: str = raw.get("description", "")
    created_at: str = raw.get("created_at", "")

    user_info: Dict[str, Any] = raw.get("user", {})
    user_handle: str = user_info.get("handle", "")

    stable = _stable_id("version", version_id)

    content_parts: List[str] = [f"Version: {label}"]
    if version_id:
        content_parts.append(f"ID: {version_id}")
    if file_key:
        content_parts.append(f"File key: {file_key}")
    if description:
        content_parts.append(f"Description: {description}")
    if user_handle:
        content_parts.append(f"Created by: {user_handle}")
    if created_at:
        content_parts.append(f"Created: {created_at}")

    metadata: Dict[str, Any] = {
        "version_id": version_id,
        "label": label,
        "file_key": file_key,
        "description": description,
        "user_handle": user_handle,
        "created_at": created_at,
        "object_type": "version",
        "source": "figma",
    }

    return ConnectorDocument(
        id=stable,
        title=label,
        content="\n".join(content_parts),
        type="figma_version",
        metadata=metadata,
    )


# ── retry ─────────────────────────────────────────────────────────────────────

async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on FigmaAuthError — re-authorizing is required.
    Skips retry on FigmaNotFoundError — the resource does not exist.
    """
    from exceptions import FigmaAuthError, FigmaError, FigmaNotFoundError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except FigmaAuthError:
            raise
        except FigmaNotFoundError:
            raise
        except FigmaError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
