"""Transforms raw Personio API responses into NormalizedDocument objects.

Personio wraps every employee attribute in `{label, value, type, …}`.
`_attr` dereferences `.value`; `_attr_str` recurses into nested
`{type, attributes: {name}}` shapes used for department/position.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from shared.base_connector import NormalizedDocument


def _attr(attrs: Dict[str, Any], key: str) -> Any:
    item = attrs.get(key) if isinstance(attrs, dict) else None
    if isinstance(item, dict) and "value" in item:
        return item["value"]
    return item


def _attr_str(attrs: Dict[str, Any], key: str) -> str:
    v = _attr(attrs, key)
    if v is None:
        return ""
    if isinstance(v, dict):
        # Nested {type, attributes: {name}} shape.
        nested = v.get("attributes") or {}
        if isinstance(nested, dict):
            return str(nested.get("name", ""))
        return ""
    return str(v)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def normalize_employee(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert a Personio employee envelope to a NormalizedDocument.

    The Shielva contract is `id = f"{tenant_id}_{source_id}"` — tenant-scoped
    so that two tenants ingesting the same Personio account never collide.
    """
    attrs = raw.get("attributes") or raw or {}

    employee_id_raw = _attr(attrs, "id") or raw.get("id") or ""
    employee_id = str(employee_id_raw)
    first = _attr_str(attrs, "first_name")
    last = _attr_str(attrs, "last_name")
    email = _attr_str(attrs, "email")
    department = _attr_str(attrs, "department")
    position = _attr_str(attrs, "position")
    hire_date = _attr_str(attrs, "hire_date")
    status = _attr_str(attrs, "status")

    full_name = f"{first} {last}".strip() or email or f"Employee {employee_id}"
    content_lines = [
        f"Name: {full_name}",
        f"Email: {email}" if email else "",
        f"Department: {department}" if department else "",
        f"Position: {position}" if position else "",
        f"Hire date: {hire_date}" if hire_date else "",
        f"Status: {status}" if status else "",
    ]
    content = "\n".join(line for line in content_lines if line)

    created_at = _parse_iso(_attr_str(attrs, "created_at")) or _parse_iso(hire_date)
    updated_at = _parse_iso(_attr_str(attrs, "last_modified_at")) or created_at

    return NormalizedDocument(
        id=f"{tenant_id}_{employee_id}",
        source_id=employee_id,
        title=full_name,
        content=content,
        content_type="text",
        source_url=f"https://app.personio.com/employees/{employee_id}",
        author=email or None,
        created_at=created_at,
        updated_at=updated_at,
        source="personio",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "first_name": first,
            "last_name": last,
            "email": email,
            "department": department,
            "position": position,
            "hire_date": hire_date,
            "status": status,
            "kind": "personio.employee",
        },
    )
