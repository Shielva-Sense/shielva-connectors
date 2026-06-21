"""Normalize Kommo API resources into NormalizedDocument."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _to_datetime(epoch: Any) -> Optional[datetime]:
    """Convert a Kommo epoch-seconds timestamp into a tz-aware datetime."""
    if epoch in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_lead(
    lead: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    subdomain: str = "",
):
    """Turn a Kommo lead into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    lead_id = str(lead.get("id", ""))
    name = lead.get("name") or f"Lead {lead_id}"
    price = lead.get("price")
    pipeline_id = lead.get("pipeline_id")
    status_id = lead.get("status_id")
    responsible_user_id = lead.get("responsible_user_id")

    content_parts = [
        f"Lead: {name}",
        f"Price: {price}" if price is not None else "",
        f"Pipeline: {pipeline_id}" if pipeline_id is not None else "",
        f"Status: {status_id}" if status_id is not None else "",
    ]
    content = "\n".join(p for p in content_parts if p)

    source_url = (
        f"https://{subdomain}.kommo.com/leads/detail/{lead_id}"
        if subdomain and lead_id
        else None
    )

    return NormalizedDocument(
        id=f"{tenant_id}_{lead_id}" if lead_id else tenant_id,
        source_id=lead_id,
        title=name,
        content=content,
        content_type="text",
        source_url=source_url,
        url=source_url,
        author=str(responsible_user_id) if responsible_user_id is not None else None,
        created_at=_to_datetime(lead.get("created_at")),
        updated_at=_to_datetime(lead.get("updated_at")),
        source="kommo",
        metadata={
            "pipeline_id": pipeline_id,
            "status_id": status_id,
            "price": price,
            "responsible_user_id": responsible_user_id,
            "kind": "kommo.lead",
        },
    )


def normalize_contact(
    contact: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    subdomain: str = "",
):
    """Turn a Kommo contact into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    contact_id = str(contact.get("id", ""))
    name = (
        contact.get("name")
        or " ".join(
            filter(None, [contact.get("first_name"), contact.get("last_name")])
        )
        or f"Contact {contact_id}"
    )

    source_url = (
        f"https://{subdomain}.kommo.com/contacts/detail/{contact_id}"
        if subdomain and contact_id
        else None
    )

    return NormalizedDocument(
        id=f"{tenant_id}_{contact_id}" if contact_id else tenant_id,
        source_id=contact_id,
        title=name,
        content=name,
        content_type="text",
        source_url=source_url,
        url=source_url,
        author=None,
        created_at=_to_datetime(contact.get("created_at")),
        updated_at=_to_datetime(contact.get("updated_at")),
        source="kommo",
        metadata={
            "first_name": contact.get("first_name"),
            "last_name": contact.get("last_name"),
            "responsible_user_id": contact.get("responsible_user_id"),
            "kind": "kommo.contact",
        },
    )
