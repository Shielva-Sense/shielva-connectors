from __future__ import annotations

from typing import Any

from models import ConnectorDocument

SF_INSTANCE_BASE = "https://salesforce.com"  # fallback; source_url is built from instance_url where available


def normalize_lead(
    record: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    instance_url: str = "",
) -> ConnectorDocument:
    """Convert a raw Salesforce Lead SObject into a ConnectorDocument."""
    record_id = record.get("Id", "")
    first_name = record.get("FirstName", "") or ""
    last_name = record.get("LastName", "") or "Unknown"
    company = record.get("Company", "") or ""
    email = record.get("Email", "") or ""
    phone = record.get("Phone", "") or ""
    status = record.get("Status", "") or ""
    lead_source = record.get("LeadSource", "") or ""
    created_date = record.get("CreatedDate", "") or ""

    full_name = f"{first_name} {last_name}".strip()
    title = f"Salesforce Lead: {full_name}" + (f" — {company}" if company else "")

    content_parts = [
        f"Lead ID: {record_id}",
        f"Name: {full_name}",
    ]
    if company:
        content_parts.append(f"Company: {company}")
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if status:
        content_parts.append(f"Status: {status}")
    if lead_source:
        content_parts.append(f"Lead Source: {lead_source}")
    if created_date:
        content_parts.append(f"Created: {created_date}")

    base = instance_url.rstrip("/") if instance_url else SF_INSTANCE_BASE
    source_url = f"{base}/lightning/r/Lead/{record_id}/view" if record_id else ""

    return ConnectorDocument(
        source_id=record_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "object_type": "Lead",
            "name": full_name,
            "company": company,
            "email": email,
            "status": status,
            "lead_source": lead_source,
            "created_date": created_date,
        },
    )


def normalize_contact(
    record: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    instance_url: str = "",
) -> ConnectorDocument:
    """Convert a raw Salesforce Contact SObject into a ConnectorDocument."""
    record_id = record.get("Id", "")
    first_name = record.get("FirstName", "") or ""
    last_name = record.get("LastName", "") or "Unknown"
    account_name = record.get("Account", {}).get("Name", "") if isinstance(record.get("Account"), dict) else ""
    email = record.get("Email", "") or ""
    phone = record.get("Phone", "") or ""
    title_field = record.get("Title", "") or ""
    created_date = record.get("CreatedDate", "") or ""

    full_name = f"{first_name} {last_name}".strip()
    title = f"Salesforce Contact: {full_name}" + (f" — {account_name}" if account_name else "")

    content_parts = [
        f"Contact ID: {record_id}",
        f"Name: {full_name}",
    ]
    if title_field:
        content_parts.append(f"Title: {title_field}")
    if account_name:
        content_parts.append(f"Account: {account_name}")
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if created_date:
        content_parts.append(f"Created: {created_date}")

    base = instance_url.rstrip("/") if instance_url else SF_INSTANCE_BASE
    source_url = f"{base}/lightning/r/Contact/{record_id}/view" if record_id else ""

    return ConnectorDocument(
        source_id=record_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "object_type": "Contact",
            "name": full_name,
            "account": account_name,
            "email": email,
            "title": title_field,
            "created_date": created_date,
        },
    )


def normalize_opportunity(
    record: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    instance_url: str = "",
) -> ConnectorDocument:
    """Convert a raw Salesforce Opportunity SObject into a ConnectorDocument."""
    record_id = record.get("Id", "")
    name = record.get("Name", "") or "Unnamed Opportunity"
    stage = record.get("StageName", "") or ""
    amount = record.get("Amount")
    close_date = record.get("CloseDate", "") or ""
    account_name = record.get("Account", {}).get("Name", "") if isinstance(record.get("Account"), dict) else ""
    probability = record.get("Probability")
    created_date = record.get("CreatedDate", "") or ""

    title = f"Salesforce Opportunity: {name}" + (f" — {stage}" if stage else "")

    content_parts = [
        f"Opportunity ID: {record_id}",
        f"Name: {name}",
    ]
    if stage:
        content_parts.append(f"Stage: {stage}")
    if amount is not None:
        content_parts.append(f"Amount: {amount}")
    if close_date:
        content_parts.append(f"Close Date: {close_date}")
    if account_name:
        content_parts.append(f"Account: {account_name}")
    if probability is not None:
        content_parts.append(f"Probability: {probability}%")
    if created_date:
        content_parts.append(f"Created: {created_date}")

    base = instance_url.rstrip("/") if instance_url else SF_INSTANCE_BASE
    source_url = f"{base}/lightning/r/Opportunity/{record_id}/view" if record_id else ""

    return ConnectorDocument(
        source_id=record_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "object_type": "Opportunity",
            "name": name,
            "stage": stage,
            "amount": amount,
            "close_date": close_date,
            "account": account_name,
            "probability": probability,
            "created_date": created_date,
        },
    )
