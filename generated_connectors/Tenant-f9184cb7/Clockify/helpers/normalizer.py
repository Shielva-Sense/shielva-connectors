"""Normalization helpers — Clockify raw payload → NormalizedDocument or local model."""
from typing import Any, Dict

from shared.base_connector import NormalizedDocument

from models import ProjectModel, TimeEntryModel


def normalize_time_entry(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert a Clockify time-entry payload into a NormalizedDocument."""
    entry_id = raw.get("id", "")
    time_interval = raw.get("timeInterval", {}) or {}
    start = time_interval.get("start", "") or ""
    end = time_interval.get("end", "") or ""
    duration = time_interval.get("duration", "") or ""
    description = raw.get("description", "") or ""
    project_id = raw.get("projectId") or ""
    workspace_id = raw.get("workspaceId") or ""
    user_id = raw.get("userId") or ""

    title = description or f"Time entry {entry_id}"
    content = (
        f"Description: {description}\n"
        f"Project: {project_id}\n"
        f"Start: {start}\n"
        f"End: {end}\n"
        f"Duration: {duration}\n"
    )
    return NormalizedDocument(
        # NormalizedDocument id MUST be tenant-scoped per the connector spec:
        # `f"{tenant_id}_{source_id}"` — ensures cross-tenant isolation in the
        # knowledge base.
        id=f"{tenant_id}_{entry_id}",
        source_id=entry_id,
        connector_id=connector_id,
        tenant_id=tenant_id,
        title=title,
        content=content,
        author=user_id,
        metadata={
            "workspace_id": workspace_id,
            "project_id": project_id,
            "billable": bool(raw.get("billable", False)),
            "tag_ids": list(raw.get("tagIds") or []),
            "start": start,
            "end": end,
            "duration": duration,
        },
    )


def normalize_project(raw: Dict[str, Any]) -> ProjectModel:
    """Convert a Clockify project payload into a ProjectModel."""
    return ProjectModel(
        id=raw.get("id", ""),
        name=raw.get("name", ""),
        workspace_id=raw.get("workspaceId", ""),
        client_id=raw.get("clientId") or None,
        archived=bool(raw.get("archived", False)),
        billable=bool(raw.get("billable", False)),
        color=raw.get("color"),
        hourly_rate=raw.get("hourlyRate"),
    )


def normalize_time_entry_model(raw: Dict[str, Any]) -> TimeEntryModel:
    """Convert a Clockify time-entry payload into a TimeEntryModel (local)."""
    interval = raw.get("timeInterval", {}) or {}
    return TimeEntryModel(
        id=raw.get("id", ""),
        workspace_id=raw.get("workspaceId", ""),
        user_id=raw.get("userId", ""),
        description=raw.get("description", "") or "",
        project_id=raw.get("projectId"),
        task_id=raw.get("taskId"),
        tag_ids=list(raw.get("tagIds") or []),
        billable=bool(raw.get("billable", False)),
        start=interval.get("start"),
        end=interval.get("end"),
        duration=interval.get("duration"),
    )
