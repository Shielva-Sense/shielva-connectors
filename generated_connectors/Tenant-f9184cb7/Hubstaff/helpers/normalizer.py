"""Normalize Hubstaff API resources into NormalizedDocument."""
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
    """Turn a Hubstaff project into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    project = raw if isinstance(raw, dict) else {}
    source_id = str(project.get("id", ""))
    name = project.get("name", "") or ""
    description = project.get("description", "") or name
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=name or f"Project {source_id}",
        content=description,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=_parse_dt(project.get("created_at")),
        updated_at=_parse_dt(project.get("updated_at") or project.get("created_at")),
        metadata={
            "status": project.get("status", ""),
            "organization_id": project.get("organization_id"),
            "billable": project.get("billable"),
            "kind": "hubstaff.project",
        },
    )


def normalize_task(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Hubstaff task into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    task = raw if isinstance(raw, dict) else {}
    source_id = str(task.get("id", ""))
    summary = task.get("summary", "") or f"Task {source_id}"
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=summary,
        content=summary,
        content_type="text",
        author=str(task.get("assignee_id", "") or ""),
        created_at=_parse_dt(task.get("created_at")),
        updated_at=_parse_dt(task.get("updated_at") or task.get("created_at")),
        metadata={
            "project_id": task.get("project_id"),
            "status": task.get("status", ""),
            "due_at": task.get("due_at"),
            "kind": "hubstaff.task",
        },
    )


def normalize_activity(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Hubstaff daily activity into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    activity = raw if isinstance(raw, dict) else {}
    source_id = str(activity.get("id", ""))
    user_id = activity.get("user_id", "")
    project_id = activity.get("project_id", "")
    title = f"Activity {source_id} — user {user_id} project {project_id}"
    content = (
        f"tracked={activity.get('tracked', 0)} "
        f"idle={activity.get('idle', 0)} "
        f"keyboard={activity.get('keyboard', 0)} "
        f"mouse={activity.get('mouse', 0)} "
        f"overall={activity.get('overall', 0)}"
    )
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        author=str(user_id or ""),
        created_at=_parse_dt(activity.get("starts_at") or activity.get("date")),
        updated_at=_parse_dt(activity.get("updated_at") or activity.get("date")),
        metadata={
            "user_id": activity.get("user_id"),
            "project_id": activity.get("project_id"),
            "task_id": activity.get("task_id"),
            "tracked": activity.get("tracked"),
            "kind": "hubstaff.daily_activity",
        },
    )
