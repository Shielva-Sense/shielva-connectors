"""Normalize Toggl Track API resources into NormalizedDocument."""
from __future__ import annotations

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


def normalize_project(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Toggl Track project into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    project = raw if isinstance(raw, dict) else {}
    source_id = str(project.get("id", ""))
    name = project.get("name", "") or ""
    client_name = project.get("client_name") or ""
    workspace_id = project.get("workspace_id") or project.get("wid")

    content = name
    if client_name:
        content = f"{name} — {client_name}"

    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=name or f"Project {source_id}",
        content=content,
        content_type="text",
        source_url=(
            f"https://track.toggl.com/{workspace_id}/projects/{source_id}"
            if workspace_id and source_id
            else None
        ),
        url=None,
        author=None,
        created_at=_parse_dt(project.get("created_at")),
        updated_at=_parse_dt(project.get("at") or project.get("updated_at")),
        source="toggl.project",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "workspace_id": workspace_id,
            "client_id": project.get("client_id"),
            "active": project.get("active"),
            "billable": project.get("billable"),
            "color": project.get("color"),
            "rate": project.get("rate"),
            "currency": project.get("currency"),
            "kind": "toggl.project",
        },
    )


def normalize_time_entry(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Toggl Track time entry into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    entry = raw if isinstance(raw, dict) else {}
    source_id = str(entry.get("id", ""))
    description = entry.get("description") or f"Time entry {source_id}"
    duration = entry.get("duration", 0)
    stop = entry.get("stop") or "(running)"

    content = f"{description} — duration {duration}s, stopped {stop}"

    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=description,
        content=content,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=_parse_dt(entry.get("start")),
        updated_at=_parse_dt(entry.get("at") or entry.get("stop")),
        source="toggl.time_entry",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "workspace_id": entry.get("workspace_id") or entry.get("wid"),
            "project_id": entry.get("project_id") or entry.get("pid"),
            "task_id": entry.get("task_id") or entry.get("tid"),
            "billable": entry.get("billable"),
            "duration": duration,
            "stop": entry.get("stop"),
            "tags": entry.get("tags", []) or [],
            "user_id": entry.get("user_id") or entry.get("uid"),
            "kind": "toggl.time_entry",
        },
    )
