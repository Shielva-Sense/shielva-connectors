"""Normalize Snyk JSON:API resources into NormalizedDocument."""
from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _attrs(entity: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(entity, dict):
        return {}
    return entity.get("attributes") or {}


def normalize_project(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Snyk REST v3 project resource into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    data = raw.get("data", raw) if isinstance(raw, dict) else {}
    if isinstance(data, list):  # collection — caller should iterate; pick first
        data = data[0] if data else {}
    attrs = _attrs(data)
    source_id = data.get("id", "")
    title = attrs.get("name", source_id)
    target_id = (
        (data.get("relationships") or {})
        .get("target", {})
        .get("data", {})
        .get("id", "")
    )
    content_parts = [
        f"Project: {title}",
        f"Type: {attrs.get('type', '')}",
        f"Origin: {attrs.get('origin', '')}",
        f"Status: {attrs.get('status', '')}",
    ]
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content="\n".join(p for p in content_parts if p),
        content_type="text",
        source="snyk.projects",
        author=None,
        created_at=_parse_dt(attrs.get("created")),
        updated_at=_parse_dt(
            attrs.get("updated") or attrs.get("created")
        ),
        metadata={
            "type": attrs.get("type", ""),
            "origin": attrs.get("origin", ""),
            "status": attrs.get("status", ""),
            "target_id": target_id,
            "kind": "snyk.project",
        },
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_issue(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Snyk REST v3 issue resource into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    data = raw.get("data", raw) if isinstance(raw, dict) else {}
    if isinstance(data, list):
        data = data[0] if data else {}
    attrs = _attrs(data)
    source_id = data.get("id", "")
    title = attrs.get("title", source_id)
    severity = (
        attrs.get("effective_severity_level")
        or attrs.get("severity", "")
    )
    issue_type = attrs.get("type", "")
    description = attrs.get("description", "") or ""
    content = "\n".join(
        p
        for p in [
            f"Issue: {title}",
            f"Severity: {severity}",
            f"Type: {issue_type}",
            description,
        ]
        if p
    )
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        source="snyk.issues",
        author=None,
        created_at=_parse_dt(attrs.get("created_at")),
        updated_at=_parse_dt(
            attrs.get("updated_at") or attrs.get("created_at")
        ),
        metadata={
            "severity": severity,
            "type": issue_type,
            "status": attrs.get("status", ""),
            "kind": "snyk.issue",
        },
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
