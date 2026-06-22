"""Normalize Bitrix24 REST resources into `NormalizedDocument`.

Bitrix24 CRM entities use UPPERCASE_SNAKE field names (`ID`, `TITLE`,
`STAGE_ID`); Tasks use lowerCamelCase (`id`, `title`, `responsibleId`).
Each normalizer accepts the raw row and emits a tenant-scoped doc id:
`id = f"{tenant_id}_{source_id}"`.
"""
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


def _first_value(items: Any) -> str:
    """Bitrix24 multi-value fields look like `[{"VALUE": "...", "VALUE_TYPE": "WORK"}]`."""
    if not items or not isinstance(items, list):
        return ""
    head = items[0]
    if isinstance(head, dict):
        return str(head.get("VALUE") or "")
    return str(head)


def normalize_lead(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Bitrix24 CRM lead into a `NormalizedDocument`."""
    from shared.base_connector import NormalizedDocument

    lead = raw if isinstance(raw, dict) else {}
    source_id = str(lead.get("ID") or "")
    title = lead.get("TITLE") or f"Lead {source_id}"
    name = " ".join(
        p for p in (lead.get("NAME"), lead.get("LAST_NAME")) if p
    ).strip()
    status = lead.get("STATUS_ID") or ""
    content = f"{name} ({status})".strip(" ()")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        author=lead.get("ASSIGNED_BY_ID"),
        created_at=_parse_dt(lead.get("DATE_CREATE")),
        updated_at=_parse_dt(lead.get("DATE_MODIFY") or lead.get("DATE_CREATE")),
        metadata={
            "status": status,
            "source": lead.get("SOURCE_ID") or "",
            "opportunity": lead.get("OPPORTUNITY"),
            "currency": lead.get("CURRENCY_ID") or "",
            "assigned_to": lead.get("ASSIGNED_BY_ID"),
            "kind": "bitrix24.lead",
        },
    )


def normalize_contact(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Bitrix24 CRM contact into a `NormalizedDocument`."""
    from shared.base_connector import NormalizedDocument

    contact = raw if isinstance(raw, dict) else {}
    source_id = str(contact.get("ID") or "")
    name = " ".join(
        p for p in (contact.get("NAME"), contact.get("LAST_NAME")) if p
    ).strip()
    title = name or f"Contact {source_id}"
    email = _first_value(contact.get("EMAIL"))
    phone = _first_value(contact.get("PHONE"))
    content = " ".join(p for p in (email, phone) if p).strip()
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        author=contact.get("ASSIGNED_BY_ID"),
        created_at=_parse_dt(contact.get("DATE_CREATE")),
        updated_at=_parse_dt(contact.get("DATE_MODIFY") or contact.get("DATE_CREATE")),
        metadata={
            "email": email,
            "phone": phone,
            "post": contact.get("POST") or "",
            "comments": contact.get("COMMENTS") or "",
            "kind": "bitrix24.contact",
        },
    )


def normalize_deal(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Bitrix24 CRM deal into a `NormalizedDocument`."""
    from shared.base_connector import NormalizedDocument

    deal = raw if isinstance(raw, dict) else {}
    source_id = str(deal.get("ID") or "")
    title = deal.get("TITLE") or f"Deal {source_id}"
    stage = deal.get("STAGE_ID") or ""
    opp = deal.get("OPPORTUNITY")
    currency = deal.get("CURRENCY_ID") or ""
    content = f"Stage {stage} — {opp} {currency}".strip(" —")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        author=deal.get("ASSIGNED_BY_ID"),
        created_at=_parse_dt(deal.get("DATE_CREATE")),
        updated_at=_parse_dt(deal.get("DATE_MODIFY") or deal.get("DATE_CREATE")),
        metadata={
            "stage": stage,
            "opportunity": opp,
            "currency": currency,
            "contact_id": deal.get("CONTACT_ID"),
            "company_id": deal.get("COMPANY_ID"),
            "kind": "bitrix24.deal",
        },
    )


def normalize_task(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Bitrix24 task into a `NormalizedDocument`.

    Tasks use lowerCamelCase keys (`id`, `title`, `responsibleId`).
    """
    from shared.base_connector import NormalizedDocument

    task = raw if isinstance(raw, dict) else {}
    source_id = str(task.get("id") or task.get("ID") or "")
    title = task.get("title") or task.get("TITLE") or f"Task {source_id}"
    description = task.get("description") or task.get("DESCRIPTION") or ""
    status = task.get("status") or task.get("STATUS") or ""
    responsible = task.get("responsibleId") or task.get("RESPONSIBLE_ID")
    deadline = task.get("deadline") or task.get("DEADLINE")
    created = task.get("createdDate") or task.get("CREATED_DATE")
    changed = task.get("changedDate") or task.get("CHANGED_DATE") or created
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=description,
        content_type="text",
        author=str(responsible) if responsible is not None else None,
        created_at=_parse_dt(created),
        updated_at=_parse_dt(changed),
        metadata={
            "status": status,
            "responsible_id": responsible,
            "deadline": deadline,
            "kind": "bitrix24.task",
        },
    )
