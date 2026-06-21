"""Normalize Workato API resources into NormalizedDocument."""
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


def normalize_recipe(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Workato recipe payload into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    recipe = raw.get("recipe", raw) if isinstance(raw, dict) else {}
    source_id = str(recipe.get("id", ""))
    name = recipe.get("name", "") or ""
    description = recipe.get("description", "") or ""
    content = description or f"{name} — running={recipe.get('running')}"
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=content,
        content_type="text",
        created_at=_parse_dt(recipe.get("created_at")),
        updated_at=_parse_dt(recipe.get("updated_at")),
        metadata={
            "running": recipe.get("running"),
            "job_succeeded_count": recipe.get("job_succeeded_count"),
            "job_failed_count": recipe.get("job_failed_count"),
            "folder_id": recipe.get("folder_id"),
            "version_no": recipe.get("version_no"),
            "kind": "workato.recipe",
        },
    )


def normalize_connection(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Workato connection payload into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    conn = raw.get("connection", raw) if isinstance(raw, dict) else {}
    source_id = str(conn.get("id", ""))
    name = conn.get("name", "") or ""
    provider = conn.get("provider", "") or conn.get("application", "")
    status = conn.get("authorization_status", "") or ""
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=f"{provider} {status}".strip(),
        content_type="text",
        created_at=_parse_dt(conn.get("created_at")),
        updated_at=_parse_dt(conn.get("updated_at")),
        metadata={
            "provider": provider,
            "application": conn.get("application", ""),
            "authorization_status": status,
            "folder_id": conn.get("folder_id"),
            "kind": "workato.connection",
        },
    )


def normalize_job(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Workato job (recipe run) payload into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    job = raw.get("job", raw) if isinstance(raw, dict) else {}
    source_id = str(job.get("id", ""))
    flow_run_id = job.get("flow_run_id", "") or ""
    status = job.get("status", "") or ""
    error = job.get("error", "") or ""
    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=f"Job {source_id} — {flow_run_id}" if flow_run_id else f"Job {source_id}",
        content=error or status,
        content_type="text",
        created_at=_parse_dt(job.get("started_at") or job.get("created_at")),
        updated_at=_parse_dt(job.get("completed_at") or job.get("started_at") or job.get("created_at")),
        metadata={
            "status": status,
            "recipe_id": job.get("recipe_id"),
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
            "kind": "workato.job",
        },
    )
