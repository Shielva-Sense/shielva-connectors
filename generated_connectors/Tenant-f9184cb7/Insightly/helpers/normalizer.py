"""Normalize Insightly API resources into NormalizedDocument.

Every NormalizedDocument id is `f"{tenant_id}_{source_id}"` so two tenants
with the same Insightly record ID produce distinct documents — multi-tenant
isolation by construction.
"""
from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            # Insightly returns UTC timestamps like "2025-01-15 12:34:56" (no Z)
            # or ISO-8601 with offset. Try ISO first, then space-separated fallback.
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _first_email(raw: Dict[str, Any]) -> str:
    for entry in raw.get("EMAILADDRESSES") or []:
        if isinstance(entry, dict) and entry.get("EMAIL_ADDRESS"):
            return str(entry["EMAIL_ADDRESS"])
    if raw.get("EMAIL_ADDRESS"):
        return str(raw["EMAIL_ADDRESS"])
    return ""


def _first_phone(raw: Dict[str, Any]) -> str:
    for entry in raw.get("CONTACTINFOS") or []:
        if isinstance(entry, dict) and entry.get("TYPE") == "PHONE":
            return str(entry.get("DETAIL", "") or "")
    if raw.get("PHONE"):
        return str(raw["PHONE"])
    return ""


def normalize_contact(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Insightly Contact → NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    contact_id = raw.get("CONTACT_ID")
    source_id = str(contact_id) if contact_id is not None else ""
    first = (raw.get("FIRST_NAME") or "").strip()
    last = (raw.get("LAST_NAME") or "").strip()
    title = f"{first} {last}".strip() or f"Contact {source_id}"
    email = _first_email(raw)
    phone = _first_phone(raw)

    lines = [f"Name: {title}"]
    if email:
        lines.append(f"Email: {email}")
    if phone:
        lines.append(f"Phone: {phone}")
    if raw.get("BACKGROUND"):
        lines.append("")
        lines.append(str(raw["BACKGROUND"]))

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content="\n".join(lines),
        content_type="text",
        source="insightly",
        author=email or None,
        created_at=_parse_dt(raw.get("DATE_CREATED_UTC")),
        updated_at=_parse_dt(raw.get("DATE_UPDATED_UTC")),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "insightly.contact",
            "organisation_id": raw.get("ORGANISATION_ID"),
            "raw": raw,
        },
    )


def normalize_organisation(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Insightly Organisation → NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    org_id = raw.get("ORGANISATION_ID")
    source_id = str(org_id) if org_id is not None else ""
    name = (raw.get("ORGANISATION_NAME") or "").strip() or f"Organisation {source_id}"
    background = raw.get("BACKGROUND") or ""

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=str(background),
        content_type="text",
        source="insightly",
        created_at=_parse_dt(raw.get("DATE_CREATED_UTC")),
        updated_at=_parse_dt(raw.get("DATE_UPDATED_UTC")),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "insightly.organisation",
            "phone": raw.get("PHONE"),
            "website": raw.get("WEBSITE"),
            "raw": raw,
        },
    )


def normalize_opportunity(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Insightly Opportunity → NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    opp_id = raw.get("OPPORTUNITY_ID")
    source_id = str(opp_id) if opp_id is not None else ""
    name = (raw.get("OPPORTUNITY_NAME") or "").strip() or f"Opportunity {source_id}"

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=str(raw.get("OPPORTUNITY_DETAILS") or ""),
        content_type="text",
        source="insightly",
        created_at=_parse_dt(raw.get("DATE_CREATED_UTC")),
        updated_at=_parse_dt(raw.get("DATE_UPDATED_UTC")),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "insightly.opportunity",
            "value": raw.get("OPPORTUNITY_VALUE"),
            "currency": raw.get("BID_CURRENCY"),
            "probability": raw.get("PROBABILITY"),
            "stage_id": raw.get("STAGE_ID"),
            "pipeline_id": raw.get("PIPELINE_ID"),
            "raw": raw,
        },
    )


def normalize_lead(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Insightly Lead → NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    lead_id = raw.get("LEAD_ID")
    source_id = str(lead_id) if lead_id is not None else ""
    first = (raw.get("FIRST_NAME") or "").strip()
    last = (raw.get("LAST_NAME") or "").strip()
    title = f"{first} {last}".strip() or f"Lead {source_id}"
    email = raw.get("EMAIL") or _first_email(raw)

    lines = [f"Name: {title}"]
    if email:
        lines.append(f"Email: {email}")
    if raw.get("ORGANISATION_NAME"):
        lines.append(f"Organisation: {raw['ORGANISATION_NAME']}")

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content="\n".join(lines),
        content_type="text",
        source="insightly",
        author=str(email) if email else None,
        created_at=_parse_dt(raw.get("DATE_CREATED_UTC")),
        updated_at=_parse_dt(raw.get("DATE_UPDATED_UTC")),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "insightly.lead",
            "lead_status_id": raw.get("LEAD_STATUS_ID"),
            "lead_source_id": raw.get("LEAD_SOURCE_ID"),
            "raw": raw,
        },
    )
