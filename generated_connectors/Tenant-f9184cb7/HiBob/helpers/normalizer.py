"""Normalize HiBob payloads into Shielva ``NormalizedDocument`` shapes."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse HiBob date strings — they're either ``YYYY-MM-DD`` or full ISO-8601."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        if isinstance(value, str) and "T" not in value:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(_safe_str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def normalize_employee(raw: Dict[str, Any], connector_id: str, tenant_id: str):
    """Project a HiBob employee dict into a Shielva NormalizedDocument.

    SOC: imported lazily so the helpers module stays usable in test runs where
    the platform SDK isn't on ``sys.path``.

    The document ``id`` is tenant-scoped (``f"{tenant_id}_{source_id}"``) per
    the multi-tenant rule — no two tenants ever collide on the same source id.
    """
    from shared.base_connector import NormalizedDocument  # type: ignore

    employee_id = _safe_str(raw.get("id") or raw.get("employeeId"))
    first_name = raw.get("firstName") or raw.get("first_name") or ""
    surname = raw.get("surname") or raw.get("lastName") or raw.get("last_name") or ""
    full_name = (raw.get("displayName") or f"{first_name} {surname}").strip() or employee_id

    work = raw.get("work") if isinstance(raw.get("work"), dict) else {}
    work_email = raw.get("workEmail") or raw.get("work_email") or ""
    personal_email = raw.get("email") or ""
    title = work.get("title") if isinstance(work, dict) else None
    department = work.get("department") if isinstance(work, dict) else None
    site = work.get("site") if isinstance(work, dict) else None
    manager = work.get("manager") if isinstance(work, dict) else None
    start_date = (work.get("startDate") if isinstance(work, dict) else None) or raw.get("startDate")

    content_parts = [
        full_name,
        personal_email,
        work_email,
        f"Title: {title}" if title else "",
        f"Department: {department}" if department else "",
        f"Site: {site}" if site else "",
    ]
    content = "\n".join(p for p in content_parts if p)

    return NormalizedDocument(
        id=f"{tenant_id}_{employee_id}",
        source_id=employee_id,
        title=full_name,
        content=content,
        content_type="text",
        source_url=None,
        url=None,
        author=full_name,
        created_at=_parse_iso(start_date),
        updated_at=_parse_iso(raw.get("modificationDate") or raw.get("updatedAt")),
        metadata={
            "email": personal_email,
            "work_email": work_email,
            "title": title or "",
            "department": department or "",
            "site": site or "",
            "manager": manager or "",
            "employee_number": raw.get("employeeNumber") or "",
            "kind": "hibob.employee",
        },
        source="hibob.people",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
