"""Normalize YouTrack API resources into NormalizedDocument."""
from datetime import datetime, timezone
from typing import Any, Dict

from helpers.utils import extract_field_value, issue_web_url


def _ts_to_dt(value: Any) -> Any:
    """YouTrack returns ms-since-epoch. Convert to aware datetime or None."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_issue(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    base_url: str = "",
):
    """Turn a YouTrack issue into a NormalizedDocument.

    Per-hard-constraint: id = ``f"{tenant_id}_{source_id}"`` (tenant-scoped).
    """
    from shared.base_connector import NormalizedDocument

    issue = raw if isinstance(raw, dict) else {}
    source_id = issue.get("id", "") or ""
    id_readable = issue.get("idReadable") or source_id
    summary = issue.get("summary") or "(no summary)"
    description = issue.get("description") or ""
    created = _ts_to_dt(issue.get("created"))
    updated = _ts_to_dt(issue.get("updated"))
    reporter = issue.get("reporter") or {}
    reporter_login = reporter.get("login") if isinstance(reporter, dict) else ""
    custom_fields = issue.get("customFields") or []

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=f"[{id_readable}] {summary}" if id_readable else summary,
        content=description,
        content_type="text",
        source_url=issue_web_url(base_url, id_readable),
        url=issue_web_url(base_url, id_readable),
        author=reporter_login or None,
        created_at=created,
        updated_at=updated or created,
        source="youtrack",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "id_readable": id_readable,
            "reporter_login": reporter_login,
            "priority": extract_field_value(custom_fields, "Priority"),
            "state": extract_field_value(custom_fields, "State"),
            "assignee": extract_field_value(custom_fields, "Assignee"),
            "type": extract_field_value(custom_fields, "Type"),
            "custom_fields": custom_fields,
            "kind": "youtrack.issue",
        },
    )
