"""Normalize Shortcut API resources into NormalizedDocument.

Multi-tenant rule: every NormalizedDocument id is ``f"{tenant_id}_{source_id}"``.
"""
from __future__ import annotations

from typing import Any, Dict

from helpers.utils import parse_iso8601, parse_iso8601_or_now


def normalize_story(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Shortcut story dict into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    story = raw if isinstance(raw, dict) else {}
    source_id = str(story.get("id", ""))
    name = story.get("name", "") or ""
    description = story.get("description", "") or ""
    requested_by_id = story.get("requested_by_id")
    author = str(requested_by_id) if requested_by_id else None

    labels = story.get("labels") or []
    label_names = [
        lbl.get("name")
        for lbl in labels
        if isinstance(lbl, dict) and lbl.get("name")
    ]

    metadata: Dict[str, Any] = {
        "story_type": story.get("story_type"),
        "workflow_state_id": story.get("workflow_state_id"),
        "project_id": story.get("project_id"),
        "epic_id": story.get("epic_id"),
        "iteration_id": story.get("iteration_id"),
        "archived": bool(story.get("archived", False)),
        "owner_ids": list(story.get("owner_ids") or []),
        "labels": label_names,
        "estimate": story.get("estimate"),
        "app_url": story.get("app_url"),
        "kind": "shortcut.story",
    }
    # Drop None-valued keys to keep the projection compact.
    metadata = {k: v for k, v in metadata.items() if v is not None}

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=description,
        content_type="markdown",
        source_url=story.get("app_url"),
        url=story.get("app_url"),
        author=author,
        created_at=parse_iso8601_or_now(story.get("created_at")),
        updated_at=parse_iso8601(story.get("updated_at")),
        metadata=metadata,
        source="shortcut",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_epic(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Shortcut epic dict into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    epic = raw if isinstance(raw, dict) else {}
    source_id = str(epic.get("id", ""))
    name = epic.get("name", "") or ""
    description = epic.get("description", "") or ""

    metadata: Dict[str, Any] = {
        "state": epic.get("state"),
        "archived": bool(epic.get("archived", False)),
        "owner_ids": list(epic.get("owner_ids") or []),
        "started": epic.get("started"),
        "completed": epic.get("completed"),
        "app_url": epic.get("app_url"),
        "kind": "shortcut.epic",
    }
    metadata = {k: v for k, v in metadata.items() if v is not None}

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=description,
        content_type="markdown",
        source_url=epic.get("app_url"),
        url=epic.get("app_url"),
        created_at=parse_iso8601_or_now(epic.get("created_at")),
        updated_at=parse_iso8601(epic.get("updated_at")),
        metadata=metadata,
        source="shortcut",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
