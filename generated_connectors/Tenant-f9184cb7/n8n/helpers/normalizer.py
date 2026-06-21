"""Normalize n8n REST API resources into ``NormalizedDocument``.

Pure functions. Zero I/O. Zero httpx. Imported by ``connector.py::sync`` only.
"""
from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    """Best-effort RFC 3339 → ``datetime``; falls back to now-UTC."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _node_summary(nodes: Any) -> str:
    """Best-effort one-line summary of workflow nodes for embedding."""
    if not isinstance(nodes, list):
        return ""
    types = []
    for n in nodes:
        if isinstance(n, dict):
            t = n.get("type") or ""
            if t:
                types.append(t)
    return ", ".join(types[:20])


def normalize_workflow(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Map a raw n8n workflow payload to a ``NormalizedDocument``."""
    from shared.base_connector import NormalizedDocument

    wf = raw or {}
    source_id = str(wf.get("id", ""))
    name = wf.get("name", "")
    tags = wf.get("tags") or []
    tag_names = [t.get("name", "") for t in tags if isinstance(t, dict)]
    nodes = wf.get("nodes") or []
    node_summary = _node_summary(nodes)

    content_parts = [name]
    if tag_names:
        content_parts.append(f"tags: {', '.join(tag_names)}")
    if node_summary:
        content_parts.append(f"nodes: {node_summary}")
    content = " | ".join(part for part in content_parts if part)

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Workflow {source_id}",
        content=content,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=_parse_dt(wf.get("createdAt")),
        updated_at=_parse_dt(wf.get("updatedAt") or wf.get("createdAt")),
        metadata={
            "active": bool(wf.get("active", False)),
            "tags": tag_names,
            "node_count": len(nodes) if isinstance(nodes, list) else 0,
            "has_trigger": any(
                isinstance(n, dict) and ("trigger" in (n.get("type") or "").lower())
                for n in nodes
            ),
            "kind": "n8n.workflow",
        },
    )


def normalize_execution(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Map a raw n8n execution payload to a ``NormalizedDocument``."""
    from shared.base_connector import NormalizedDocument

    ex = raw or {}
    source_id = str(ex.get("id", ""))
    workflow_id = ex.get("workflowId", "")
    status = ex.get("status", "")
    finished = bool(ex.get("finished", False))
    started_at = ex.get("startedAt")
    stopped_at = ex.get("stoppedAt")

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=f"Execution {source_id}",
        content=f"status={status or 'unknown'} workflow={workflow_id}",
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=_parse_dt(started_at),
        updated_at=_parse_dt(stopped_at or started_at),
        metadata={
            "workflow_id": workflow_id,
            "status": status,
            "finished": finished,
            "started_at": started_at,
            "stopped_at": stopped_at,
            "kind": "n8n.execution",
        },
    )
