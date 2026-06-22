"""Transforms raw Grafana API responses into NormalizedDocument objects."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import structlog
from shared.base_connector import NormalizedDocument

logger = structlog.get_logger(__name__)


def _parse_grafana_time(value: Any) -> Optional[datetime]:
    """Grafana returns ISO 8601 strings (e.g. '2024-01-02T03:04:05Z')."""
    if not value or not isinstance(value, str):
        return None
    try:
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return None


def normalize_dashboard(
    search_hit: Dict[str, Any],
    full_dashboard: Optional[Dict[str, Any]],
    connector_id: str,
    tenant_id: str,
    base_url: str = "",
) -> NormalizedDocument:
    """Convert a dashboard search hit + full dashboard response into a NormalizedDocument."""
    uid = search_hit.get("uid", "")
    title = search_hit.get("title", "") or "(untitled dashboard)"
    tags = search_hit.get("tags", []) or []
    folder_title = search_hit.get("folderTitle", "")
    url_path = search_hit.get("url", "")
    source_url = f"{base_url.rstrip('/')}{url_path}" if (base_url and url_path) else url_path

    panels_summary: list[str] = []
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    if full_dashboard:
        meta = full_dashboard.get("meta", {}) or {}
        dash = full_dashboard.get("dashboard", {}) or {}
        created_at = _parse_grafana_time(meta.get("created"))
        updated_at = _parse_grafana_time(meta.get("updated"))
        for panel in dash.get("panels", []) or []:
            ptitle = panel.get("title")
            if ptitle:
                panels_summary.append(str(ptitle))

    content = "\n".join(
        [
            f"Dashboard: {title}",
            f"Folder: {folder_title}" if folder_title else "",
            f"Tags: {', '.join(tags)}" if tags else "",
            f"Panels: {', '.join(panels_summary)}" if panels_summary else "",
        ]
    ).strip()

    return NormalizedDocument(
        id=f"{tenant_id}_{uid}",
        source_id=uid,
        title=title,
        content=content,
        content_type="text",
        source_url=source_url,
        author="",
        created_at=created_at,
        updated_at=updated_at,
        source="grafana",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "tags": tags,
            "folder_title": folder_title,
            "folder_uid": search_hit.get("folderUid"),
            "panels": panels_summary,
            "type": search_hit.get("type"),
        },
    )
