"""Normalize Drata API resources into NormalizedDocument."""
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


def _person_name(person: Dict[str, Any]) -> str:
    first = person.get("firstName") or ""
    last = person.get("lastName") or ""
    full = (first + " " + last).strip()
    return full or person.get("name") or person.get("email") or person.get("id", "")


def normalize_personnel(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str,
):
    """Turn a Drata personnel record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    person = raw if isinstance(raw, dict) else {}
    source_id = str(person.get("id", ""))
    name = _person_name(person)
    email = person.get("email", "")
    role = person.get("role") or person.get("employmentType") or ""
    status = person.get("status", "")

    content_lines = [
        f"Name: {name}",
        f"Email: {email}",
        f"Role: {role}",
        f"Status: {status}",
    ]
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content="\n".join(content_lines),
        content_type="text",
        author=email or None,
        created_at=_parse_dt(person.get("createdAt")),
        updated_at=_parse_dt(person.get("updatedAt")),
        metadata={
            "status": status,
            "role": role,
            "employmentType": person.get("employmentType", ""),
            "email": email,
            "kind": "drata.personnel",
        },
    )


def normalize_control(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str,
):
    """Turn a Drata control into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    control = raw if isinstance(raw, dict) else {}
    source_id = str(control.get("id", ""))
    name = control.get("name", "")
    description = control.get("description", "") or ""
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Control {source_id}",
        content=description,
        content_type="text",
        created_at=_parse_dt(control.get("createdAt")),
        updated_at=_parse_dt(control.get("updatedAt")),
        metadata={
            "status": control.get("status", ""),
            "frameworkIds": control.get("frameworkIds", []) or control.get("frameworks", []),
            "owner": control.get("owner") or control.get("ownerId", ""),
            "kind": "drata.control",
        },
    )


def normalize_evidence(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str,
):
    """Turn a Drata evidence item into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    ev = raw if isinstance(raw, dict) else {}
    source_id = str(ev.get("id", ""))
    name = ev.get("name", "") or f"Evidence {source_id}"
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=ev.get("description", "") or "",
        content_type="text",
        created_at=_parse_dt(ev.get("createdAt")),
        updated_at=_parse_dt(ev.get("updatedAt")),
        metadata={
            "status": ev.get("status", ""),
            "controlIds": ev.get("controlIds", []),
            "kind": "drata.evidence",
        },
    )


def normalize_risk(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str,
):
    """Turn a Drata risk register entry into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    risk = raw if isinstance(raw, dict) else {}
    source_id = str(risk.get("id", ""))
    name = risk.get("name", "") or f"Risk {source_id}"
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=risk.get("description", "") or "",
        content_type="text",
        created_at=_parse_dt(risk.get("createdAt")),
        updated_at=_parse_dt(risk.get("updatedAt")),
        metadata={
            "severity": risk.get("severity", ""),
            "likelihood": risk.get("likelihood", ""),
            "status": risk.get("status", ""),
            "kind": "drata.risk",
        },
    )


def normalize_vendor(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str,
):
    """Turn a Drata vendor into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    vendor = raw if isinstance(raw, dict) else {}
    source_id = str(vendor.get("id", ""))
    name = vendor.get("name", "") or f"Vendor {source_id}"
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=vendor.get("description", "") or "",
        content_type="text",
        created_at=_parse_dt(vendor.get("createdAt")),
        updated_at=_parse_dt(vendor.get("updatedAt")),
        metadata={
            "category": vendor.get("category", ""),
            "riskLevel": vendor.get("riskLevel", ""),
            "status": vendor.get("status", ""),
            "kind": "drata.vendor",
        },
    )
