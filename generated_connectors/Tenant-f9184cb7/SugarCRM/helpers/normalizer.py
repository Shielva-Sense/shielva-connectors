"""Normalize SugarCRM REST records into ``NormalizedDocument``.

SOC: this module owns the SugarCRM-record → ``NormalizedDocument`` mapping
for the small set of modules the connector chooses to project into the
Shielva KB on ``sync()``:

* **Contacts** — ``first_name`` + ``last_name`` + primary email/phone
* **Accounts** — ``name`` + ``description`` + industry/website
* **Opportunities** — ``name`` + ``amount`` + ``sales_stage``
* **Leads** — first/last + status + lead_source
* **Cases** — ``name`` + ``description`` + status/priority

Multi-tenant: every ``NormalizedDocument.id`` is ``f"{tenant_id}_{source_id}"``
so two tenants with the same SugarCRM record ID produce distinct documents.

The connector body imports these helpers but is free to skip any module
that the tenant has not opted into via ``self.config["sync_modules"]``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from shared.base_connector import NormalizedDocument


def _parse_dt(value: Any) -> datetime:
    """Best-effort parse a SugarCRM ISO-8601 timestamp; fallback to utcnow."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            # SugarCRM emits e.g. "2026-06-21T10:11:12+00:00" or with "Z" suffix.
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _primary_email(record: Dict[str, Any]) -> str:
    """Return the SugarCRM ``email`` array primary address, or ``email1`` fallback."""
    emails = record.get("email")
    if isinstance(emails, list):
        for entry in emails:
            if isinstance(entry, dict) and entry.get("primary_address") in (True, "1", "true"):
                addr = entry.get("email_address") or ""
                if addr:
                    return addr
        if emails and isinstance(emails[0], dict):
            return emails[0].get("email_address", "") or ""
    return record.get("email1", "") or ""


def _full_name(record: Dict[str, Any]) -> str:
    """Concatenate ``first_name`` + ``last_name`` (either may be missing)."""
    first = (record.get("first_name") or "").strip()
    last = (record.get("last_name") or "").strip()
    return (first + " " + last).strip()


def _doc(
    *,
    tenant_id: str,
    connector_id: str,
    source_id: str,
    title: str,
    content: str,
    kind: str,
    record: Dict[str, Any],
    extra_meta: Dict[str, Any],
    tags: List[str],
) -> NormalizedDocument:
    """Build a ``NormalizedDocument`` with the canonical SugarCRM shape.

    ``NormalizedDocument`` itself has no ``tags`` field; we fold tags into
    ``metadata["tags"]`` so downstream KB code that filters on tags still
    finds them.
    """
    meta: Dict[str, Any] = {
        "kind": kind,
        "module": kind.split(".", 1)[-1],
        "tags": list(tags),
        "assigned_user_id": record.get("assigned_user_id"),
        "assigned_user_name": record.get("assigned_user_name"),
    }
    meta.update(extra_meta)
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=str(source_id),
        title=title or source_id,
        content=content or "",
        content_type="text",
        source_url=None,
        author=record.get("created_by_name") or record.get("modified_by_name"),
        created_at=_parse_dt(record.get("date_entered") or record.get("created_at")),
        updated_at=_parse_dt(record.get("date_modified") or record.get("updated_at")),
        metadata=meta,
        source="sugarcrm",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_contact(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Turn a SugarCRM Contact into a ``NormalizedDocument``."""
    source_id = str(raw.get("id") or "")
    name = _full_name(raw) or raw.get("name") or "Contact"
    email = _primary_email(raw)
    phone = raw.get("phone_work") or raw.get("phone_mobile") or ""
    title = raw.get("title") or ""
    department = raw.get("department") or ""
    account = raw.get("account_name") or ""
    content = " ".join(
        [name, title, department, account, email, phone, raw.get("description") or ""]
    ).strip()
    return _doc(
        tenant_id=tenant_id,
        connector_id=connector_id,
        source_id=source_id,
        title=name,
        content=content,
        kind="sugarcrm.contact",
        record=raw,
        extra_meta={
            "email": email,
            "phone": phone,
            "title": title,
            "department": department,
            "account_name": account,
        },
        tags=["sugarcrm", "contact"],
    )


def normalize_account(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Turn a SugarCRM Account into a ``NormalizedDocument``."""
    source_id = str(raw.get("id") or "")
    name = raw.get("name") or "Account"
    industry = raw.get("industry") or ""
    website = raw.get("website") or ""
    description = raw.get("description") or ""
    content = " ".join([name, industry, website, description]).strip()
    return _doc(
        tenant_id=tenant_id,
        connector_id=connector_id,
        source_id=source_id,
        title=name,
        content=content,
        kind="sugarcrm.account",
        record=raw,
        extra_meta={
            "industry": industry,
            "website": website,
            "phone_office": raw.get("phone_office", ""),
            "annual_revenue": raw.get("annual_revenue"),
        },
        tags=["sugarcrm", "account"],
    )


def normalize_opportunity(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Turn a SugarCRM Opportunity into a ``NormalizedDocument``."""
    source_id = str(raw.get("id") or "")
    name = raw.get("name") or "Opportunity"
    amount = raw.get("amount") or raw.get("amount_usdollar") or 0
    sales_stage = raw.get("sales_stage") or ""
    description = raw.get("description") or ""
    content = " ".join([name, sales_stage, str(amount), description]).strip()
    return _doc(
        tenant_id=tenant_id,
        connector_id=connector_id,
        source_id=source_id,
        title=name,
        content=content,
        kind="sugarcrm.opportunity",
        record=raw,
        extra_meta={
            "amount": amount,
            "sales_stage": sales_stage,
            "date_closed": raw.get("date_closed"),
            "account_id": raw.get("account_id"),
            "account_name": raw.get("account_name"),
            "probability": raw.get("probability"),
        },
        tags=["sugarcrm", "opportunity"],
    )


def normalize_lead(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Turn a SugarCRM Lead into a ``NormalizedDocument``."""
    source_id = str(raw.get("id") or "")
    name = _full_name(raw) or "Lead"
    status = raw.get("status") or ""
    lead_source = raw.get("lead_source") or ""
    email = _primary_email(raw)
    company = raw.get("account_name") or ""
    content = " ".join([name, company, status, lead_source, email]).strip()
    return _doc(
        tenant_id=tenant_id,
        connector_id=connector_id,
        source_id=source_id,
        title=name,
        content=content,
        kind="sugarcrm.lead",
        record=raw,
        extra_meta={
            "status": status,
            "lead_source": lead_source,
            "email": email,
            "company": company,
            "converted": raw.get("converted"),
        },
        tags=["sugarcrm", "lead"],
    )


def normalize_case(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Turn a SugarCRM Case into a ``NormalizedDocument``."""
    source_id = str(raw.get("id") or "")
    name = raw.get("name") or "Case"
    status = raw.get("status") or ""
    priority = raw.get("priority") or ""
    description = raw.get("description") or ""
    resolution = raw.get("resolution") or ""
    content = " ".join([name, status, priority, description, resolution]).strip()
    return _doc(
        tenant_id=tenant_id,
        connector_id=connector_id,
        source_id=source_id,
        title=name,
        content=content,
        kind="sugarcrm.case",
        record=raw,
        extra_meta={
            "status": status,
            "priority": priority,
            "case_number": raw.get("case_number"),
            "account_id": raw.get("account_id"),
            "account_name": raw.get("account_name"),
        },
        tags=["sugarcrm", "case"],
    )


_NORMALIZERS = {
    "Contacts": normalize_contact,
    "Accounts": normalize_account,
    "Opportunities": normalize_opportunity,
    "Leads": normalize_lead,
    "Cases": normalize_case,
}


def normalize_record(
    module: str,
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Dispatch helper used by ``sync()`` — module name → typed normaliser."""
    fn = _NORMALIZERS.get(module)
    if fn is None:
        # Unknown module — emit a generic doc so the record is still indexed.
        source_id = str(raw.get("id") or "")
        title = raw.get("name") or _full_name(raw) or module
        return _doc(
            tenant_id=tenant_id,
            connector_id=connector_id,
            source_id=source_id,
            title=title,
            content=raw.get("description") or "",
            kind=f"sugarcrm.{module.lower()}",
            record=raw,
            extra_meta={},
            tags=["sugarcrm", module.lower()],
        )
    return fn(raw, tenant_id=tenant_id, connector_id=connector_id)
