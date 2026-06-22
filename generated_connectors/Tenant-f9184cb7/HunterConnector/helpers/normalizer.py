"""Normalize Hunter.io API resources into NormalizedDocument."""
from __future__ import annotations

from typing import Any, Dict

from helpers.utils import parse_dt


def _lead_title(lead: Dict[str, Any]) -> str:
    first = (lead.get("first_name") or "").strip()
    last = (lead.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    if name:
        return name
    return lead.get("email") or "Lead"


def _lead_content(lead: Dict[str, Any]) -> str:
    bits = []
    if lead.get("position"):
        bits.append(str(lead["position"]))
    if lead.get("company"):
        bits.append(f"at {lead['company']}")
    if lead.get("email"):
        bits.append(f"<{lead['email']}>")
    if lead.get("phone_number"):
        bits.append(f"tel:{lead['phone_number']}")
    return " ".join(bits)


def normalize_lead(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Hunter lead payload into a NormalizedDocument.

    `id` is tenant-scoped: `f"{tenant_id}_{source_id}"`.
    """
    from shared.base_connector import NormalizedDocument

    lead = raw.get("lead", raw) if isinstance(raw, dict) else {}
    source_id = str(lead.get("id", ""))
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=_lead_title(lead),
        content=_lead_content(lead),
        content_type="text",
        source_url=None,
        url=None,
        author=lead.get("email"),
        created_at=parse_dt(lead.get("created_at")),
        updated_at=parse_dt(lead.get("last_activity_at") or lead.get("updated_at")),
        metadata={
            "email": lead.get("email"),
            "company": lead.get("company"),
            "position": lead.get("position"),
            "phone_number": lead.get("phone_number"),
            "twitter": lead.get("twitter"),
            "linkedin_url": lead.get("linkedin_url"),
            "source": lead.get("source"),
            "lead_list_id": lead.get("leads_list_id") or lead.get("lead_list_id"),
            "kind": "hunter.lead",
        },
    )
