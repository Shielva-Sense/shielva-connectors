from __future__ import annotations

from typing import Any

from helpers.utils import stable_id
from models import ConnectorDocument


def normalize_contact(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw ActiveCampaign Contact object into a ConnectorDocument."""
    contact_id = str(record.get("id", ""))
    first = record.get("firstName", "") or record.get("firstname", "") or ""
    last = record.get("lastName", "") or record.get("lastname", "") or ""
    name = f"{first} {last}".strip() or "Unknown"
    email = record.get("email", "") or ""
    phone = record.get("phone", "") or ""
    org_name = record.get("orgname", "") or record.get("organization", "") or ""
    created = record.get("cdate", "") or record.get("created_timestamp", "") or ""
    updated = record.get("udate", "") or record.get("updated_timestamp", "") or ""

    title = f"ActiveCampaign contact: {name}" + (f" <{email}>" if email else "")
    content_parts = [
        f"Contact ID: {contact_id}",
        f"Name: {name}",
        f"Email: {email}",
        f"Phone: {phone}",
        f"Organization: {org_name}",
        f"Created: {created}",
        f"Last updated: {updated}",
    ]

    return ConnectorDocument(
        source_id=stable_id("contact", contact_id),
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "contact",
            "ac_id": contact_id,
            "email": email,
            "name": name,
            "phone": phone,
            "organization": org_name,
            "cdate": created,
            "udate": updated,
        },
    )


def normalize_deal(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw ActiveCampaign Deal object into a ConnectorDocument."""
    deal_id = str(record.get("id", ""))
    title_val = record.get("title", "") or f"Deal {deal_id}"
    value = record.get("value", "") or ""
    currency = record.get("currency", "") or ""
    status_raw = record.get("status", "")
    # AC status: 0=open, 1=won, 2=lost
    status_map = {"0": "open", "1": "won", "2": "lost", 0: "open", 1: "won", 2: "lost"}
    status = status_map.get(status_raw, str(status_raw))
    stage = record.get("stage", "") or ""
    owner = record.get("owner", "") or ""
    created = record.get("cdate", "") or ""
    updated = record.get("mdate", "") or ""

    value_display = f"{value} {currency}".strip() if value else "N/A"
    doc_title = f"ActiveCampaign deal: {title_val} — {status}"
    content_parts = [
        f"Deal ID: {deal_id}",
        f"Title: {title_val}",
        f"Value: {value_display}",
        f"Status: {status}",
        f"Stage: {stage}",
        f"Owner: {owner}",
        f"Created: {created}",
        f"Updated: {updated}",
    ]

    return ConnectorDocument(
        source_id=stable_id("deal", deal_id),
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "deal",
            "ac_id": deal_id,
            "title": title_val,
            "value": value,
            "currency": currency,
            "status": status,
            "stage": stage,
            "owner": owner,
            "cdate": created,
            "mdate": updated,
        },
    )


def normalize_campaign(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw ActiveCampaign Campaign object into a ConnectorDocument."""
    campaign_id = str(record.get("id", ""))
    name = record.get("name", "") or f"Campaign {campaign_id}"
    ctype = record.get("type", "") or ""
    status_raw = record.get("status", "")
    status_map = {"0": "draft", "1": "scheduled", "2": "sending", "3": "sent", "4": "paused"}
    status = status_map.get(str(status_raw), str(status_raw))
    subject = record.get("subject", "") or ""
    send_amt = record.get("send_amt", "") or ""
    opens = record.get("opens", "") or ""
    created = record.get("cdate", "") or ""

    doc_title = f"ActiveCampaign campaign: {name} ({status})"
    content_parts = [
        f"Campaign ID: {campaign_id}",
        f"Name: {name}",
        f"Type: {ctype}",
        f"Status: {status}",
        f"Subject: {subject}",
        f"Sent to: {send_amt}",
        f"Opens: {opens}",
        f"Created: {created}",
    ]

    return ConnectorDocument(
        source_id=stable_id("campaign", campaign_id),
        title=doc_title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "object_type": "campaign",
            "ac_id": campaign_id,
            "name": name,
            "type": ctype,
            "status": status,
            "subject": subject,
            "send_amt": send_amt,
            "opens": opens,
            "cdate": created,
        },
    )
