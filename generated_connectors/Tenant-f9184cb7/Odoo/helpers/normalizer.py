"""Normalize raw Odoo records into canonical NormalizedDocument objects."""
from datetime import datetime, timezone
from typing import Any, Dict

from shared.base_connector import NormalizedDocument


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            # Odoo timestamps look like "2026-06-21 12:34:56" (UTC) — also accept ISO.
            normalised = value.replace("Z", "+00:00")
            if " " in normalised and "T" not in normalised:
                normalised = normalised.replace(" ", "T")
            dt = datetime.fromisoformat(normalised)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _deep_link(base_url: str, partner_id: Any) -> str:
    if not base_url or partner_id in (None, ""):
        return ""
    return (
        f"{base_url.rstrip('/')}/web#id={partner_id}"
        f"&model=res.partner&view_type=form"
    )


def normalize_partner(
    record: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    base_url: str = "",
) -> NormalizedDocument:
    """Turn a single ``res.partner`` record into a NormalizedDocument."""
    source_id = str(record.get("id", "") or "")
    name: str = record.get("name") or f"Partner #{source_id or 'unknown'}"
    email: str = record.get("email") or ""
    phone: str = record.get("phone") or ""
    is_company: bool = bool(record.get("is_company"))
    company_label = "Company" if is_company else "Individual"

    lines = [f"Name: {name}", f"Type: {company_label}"]
    if email:
        lines.append(f"Email: {email}")
    if phone:
        lines.append(f"Phone: {phone}")
    content = "\n".join(lines)

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=content,
        content_type="text",
        source_url=_deep_link(base_url, source_id) or None,
        author=email or None,
        created_at=_parse_dt(record.get("create_date")),
        updated_at=_parse_dt(record.get("write_date") or record.get("create_date")),
        metadata={
            "model": "res.partner",
            "partner_id": record.get("id"),
            "email": email,
            "phone": phone,
            "is_company": is_company,
            "connector_id": connector_id,
            "tenant_id": tenant_id,
            "kind": "odoo.partner",
        },
    )


def normalize_lead(
    record: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    base_url: str = "",
) -> NormalizedDocument:
    """Turn a single ``crm.lead`` record into a NormalizedDocument."""
    source_id = str(record.get("id", "") or "")
    name: str = record.get("name") or f"Lead #{source_id or 'unknown'}"
    description: str = record.get("description") or ""
    email: str = record.get("email_from") or ""
    expected_revenue = record.get("expected_revenue") or 0
    stage = record.get("stage_id") or ""

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=description or name,
        content_type="text",
        source_url=(
            f"{base_url.rstrip('/')}/web#id={source_id}&model=crm.lead&view_type=form"
            if base_url and source_id
            else None
        ),
        author=email or None,
        created_at=_parse_dt(record.get("create_date")),
        updated_at=_parse_dt(record.get("write_date") or record.get("create_date")),
        metadata={
            "model": "crm.lead",
            "lead_id": record.get("id"),
            "email_from": email,
            "expected_revenue": expected_revenue,
            "stage_id": stage,
            "connector_id": connector_id,
            "tenant_id": tenant_id,
            "kind": "odoo.lead",
        },
    )
