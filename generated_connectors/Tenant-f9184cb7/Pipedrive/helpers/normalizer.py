from __future__ import annotations

from typing import Any

from helpers.utils import stable_id
from models import ConnectorDocument


def normalize_deal(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw Pipedrive Deal object into a ConnectorDocument."""
    deal_id = str(record.get("id", ""))
    title_raw = record.get("title", "") or f"Deal {deal_id}"
    value = str(record.get("value", "") or "")
    currency = str(record.get("currency", "") or "")
    status = str(record.get("status", "") or "")
    stage_name = ""
    stage = record.get("stage_id")
    if isinstance(stage, dict):
        stage_name = stage.get("name", "")
    pipeline_name = ""
    pipeline = record.get("pipeline_id")
    if isinstance(pipeline, dict):
        pipeline_name = pipeline.get("name", "")
    owner_name = ""
    owner = record.get("owner_name") or ""
    if owner:
        owner_name = str(owner)
    add_time = str(record.get("add_time", "") or "")
    close_time = str(record.get("close_time", "") or "")
    expected_close = str(record.get("expected_close_date", "") or "")
    person_name = ""
    person = record.get("person_id")
    if isinstance(person, dict):
        person_name = person.get("name", "")
    org_name = ""
    org = record.get("org_id")
    if isinstance(org, dict):
        org_name = org.get("name", "")

    title = f"Pipedrive deal: {title_raw}"
    value_display = f"{value} {currency}".strip() if value else "N/A"
    content_parts = [
        f"Deal ID: {deal_id}",
        f"Title: {title_raw}",
        f"Value: {value_display}",
        f"Status: {status}",
        f"Stage: {stage_name}",
        f"Pipeline: {pipeline_name}",
        f"Owner: {owner_name}",
        f"Person: {person_name}",
        f"Organization: {org_name}",
        f"Expected close: {expected_close}",
        f"Close date: {close_time}",
        f"Created: {add_time}",
    ]

    src_id = stable_id("deal", deal_id)
    return ConnectorDocument(
        source_id=src_id,
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.pipedrive.com/deal/{deal_id}",
        metadata={
            "object_type": "deal",
            "deal_id": deal_id,
            "title": title_raw,
            "value": value,
            "currency": currency,
            "status": status,
            "stage": stage_name,
            "pipeline": pipeline_name,
            "owner": owner_name,
            "person": person_name,
            "organization": org_name,
            "add_time": add_time,
            "close_time": close_time,
            "expected_close_date": expected_close,
        },
    )


def normalize_person(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw Pipedrive Person object into a ConnectorDocument."""
    person_id = str(record.get("id", ""))
    name = str(record.get("name", "") or f"Person {person_id}")

    # Emails — Pipedrive returns a list of {value, label, primary}
    emails_raw = record.get("email", []) or []
    primary_email = ""
    if isinstance(emails_raw, list) and emails_raw:
        for em in emails_raw:
            if isinstance(em, dict) and em.get("primary"):
                primary_email = em.get("value", "")
                break
        if not primary_email and emails_raw:
            first_em = emails_raw[0]
            primary_email = first_em.get("value", "") if isinstance(first_em, dict) else str(first_em)

    # Phones
    phones_raw = record.get("phone", []) or []
    primary_phone = ""
    if isinstance(phones_raw, list) and phones_raw:
        for ph in phones_raw:
            if isinstance(ph, dict) and ph.get("primary"):
                primary_phone = ph.get("value", "")
                break
        if not primary_phone and phones_raw:
            first_ph = phones_raw[0]
            primary_phone = first_ph.get("value", "") if isinstance(first_ph, dict) else str(first_ph)

    org_name = ""
    org = record.get("org_id")
    if isinstance(org, dict):
        org_name = org.get("name", "")
    elif org:
        org_name = str(org)

    owner_name = str(record.get("owner_name", "") or "")
    add_time = str(record.get("add_time", "") or "")
    update_time = str(record.get("update_time", "") or "")

    title = f"Pipedrive person: {name}" + (f" <{primary_email}>" if primary_email else "")
    content_parts = [
        f"Person ID: {person_id}",
        f"Name: {name}",
        f"Email: {primary_email}",
        f"Phone: {primary_phone}",
        f"Organization: {org_name}",
        f"Owner: {owner_name}",
        f"Created: {add_time}",
        f"Last updated: {update_time}",
    ]

    src_id = stable_id("person", person_id)
    return ConnectorDocument(
        source_id=src_id,
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.pipedrive.com/person/{person_id}",
        metadata={
            "object_type": "person",
            "person_id": person_id,
            "name": name,
            "email": primary_email,
            "phone": primary_phone,
            "organization": org_name,
            "owner": owner_name,
            "add_time": add_time,
            "update_time": update_time,
        },
    )


def normalize_organization(
    record: dict[str, Any], connector_id: str, tenant_id: str
) -> ConnectorDocument:
    """Convert a raw Pipedrive Organization object into a ConnectorDocument."""
    org_id = str(record.get("id", ""))
    name = str(record.get("name", "") or f"Organization {org_id}")
    address = str(record.get("address", "") or "")
    owner_name = str(record.get("owner_name", "") or "")
    add_time = str(record.get("add_time", "") or "")
    update_time = str(record.get("update_time", "") or "")
    people_count = str(record.get("people_count", "") or "")
    open_deals_count = str(record.get("open_deals_count", "") or "")

    title = f"Pipedrive organization: {name}"
    content_parts = [
        f"Organization ID: {org_id}",
        f"Name: {name}",
        f"Address: {address}",
        f"Owner: {owner_name}",
        f"People count: {people_count}",
        f"Open deals: {open_deals_count}",
        f"Created: {add_time}",
        f"Last updated: {update_time}",
    ]

    src_id = stable_id("organization", org_id)
    return ConnectorDocument(
        source_id=src_id,
        title=title,
        content="\n".join(p for p in content_parts if p.split(": ", 1)[-1]),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.pipedrive.com/organization/{org_id}",
        metadata={
            "object_type": "organization",
            "org_id": org_id,
            "name": name,
            "address": address,
            "owner": owner_name,
            "people_count": people_count,
            "open_deals_count": open_deals_count,
            "add_time": add_time,
            "update_time": update_time,
        },
    )
