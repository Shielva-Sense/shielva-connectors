"""Raw Azure DevOps payloads → NormalizedDocument."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.base_connector import NormalizedDocument


def normalize_work_item(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert an Azure DevOps work item payload into a NormalizedDocument.

    NormalizedDocument.id follows the project rule:
        f"{tenant_id}_{source_id}"
    """
    fields = raw.get("fields", {}) or {}
    work_item_id = str(raw.get("id", ""))

    title = fields.get("System.Title", "") or f"Work item {work_item_id}"
    description = (
        fields.get("System.Description")
        or fields.get("Microsoft.VSTS.TCM.ReproSteps")
        or ""
    )
    created_by = fields.get("System.CreatedBy")
    if isinstance(created_by, dict):
        author = created_by.get("displayName", "") or created_by.get("uniqueName", "")
    else:
        author = created_by or ""

    created_raw = fields.get("System.CreatedDate")
    updated_raw = fields.get("System.ChangedDate")

    return NormalizedDocument(
        id=f"{tenant_id}_{work_item_id}",
        source_id=work_item_id,
        tenant_id=tenant_id,
        connector_id=connector_id,
        title=title,
        content=description if isinstance(description, str) else str(description),
        content_type="text",
        author=author if isinstance(author, str) else str(author),
        url=(raw.get("_links", {}).get("html", {}) or {}).get("href", ""),
        created_at=_parse_iso(created_raw),
        updated_at=_parse_iso(updated_raw),
        metadata={
            "rev": raw.get("rev"),
            "state": fields.get("System.State"),
            "work_item_type": fields.get("System.WorkItemType"),
            "project": fields.get("System.TeamProject"),
            "area_path": fields.get("System.AreaPath"),
            "iteration_path": fields.get("System.IterationPath"),
            "kind": "azure_devops.work_item",
        },
    )


def _parse_iso(value: Any) -> Optional[datetime]:
    """Best-effort parse of ISO-8601 timestamps (Azure DevOps uses Z suffix)."""
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        return None
