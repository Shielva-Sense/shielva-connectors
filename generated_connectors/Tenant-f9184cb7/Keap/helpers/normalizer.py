"""Normalizers — raw Keap JSON → ``shared.base_connector.NormalizedDocument``.

Every connector exposes a stable ``NormalizedDocument`` shape so the gateway
can index, search, and surface Keap records alongside other tenants' data
without leaking provider-specific keys into downstream services.

NormalizedDocument.id is tenant-scoped: ``f"{tenant_id}_{source_id}"``. The
``tenant_id`` is sourced from the connector's auth context — never an env var
or hardcoded constant.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from shared.base_connector import NormalizedDocument


def _first_email(contact: Dict[str, Any]) -> str:
    emails = contact.get("email_addresses") or []
    for e in emails:
        if isinstance(e, dict) and e.get("email"):
            return str(e["email"])
    return ""


def _full_name(contact: Dict[str, Any]) -> str:
    parts: List[str] = []
    for k in ("given_name", "middle_name", "family_name"):
        v = contact.get(k)
        if isinstance(v, str) and v:
            parts.append(v)
    return " ".join(parts).strip()


def normalize_contact(
    contact: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Normalize a Keap contact (``GET /contacts/{id}`` shape) → document."""
    source_id = str(contact.get("id") or "")
    name = _full_name(contact) or "(unnamed contact)"
    email = _first_email(contact)
    title = f"{name} <{email}>" if email else name
    content_parts: List[str] = [name]
    if email:
        content_parts.append(f"Email: {email}")
    company = contact.get("company") or {}
    if isinstance(company, dict) and company.get("company_name"):
        content_parts.append(f"Company: {company['company_name']}")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        source="keap.contacts",
        connector_id=connector_id,
        tenant_id=tenant_id,
        metadata={
            "email": email,
            "given_name": contact.get("given_name", ""),
            "family_name": contact.get("family_name", ""),
            "company": company.get("company_name", "") if isinstance(company, dict) else "",
            "tag_ids": [t.get("id") for t in (contact.get("tag_ids") or []) if isinstance(t, dict)],
        },
    )


def normalize_opportunity(
    opp: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Normalize a Keap opportunity → document."""
    source_id = str(opp.get("id") or "")
    title = opp.get("opportunity_title") or f"Opportunity {source_id}"
    stage = (opp.get("stage") or {}).get("name") if isinstance(opp.get("stage"), dict) else ""
    revenue = opp.get("projected_revenue", 0)
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=f"{title} — stage: {stage}, projected: {revenue}",
        source="keap.opportunities",
        connector_id=connector_id,
        tenant_id=tenant_id,
        metadata={
            "stage": stage,
            "projected_revenue": revenue,
            "contact_id": (opp.get("contact") or {}).get("id"),
        },
    )


def normalize_order(
    order: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Normalize a Keap order → document."""
    source_id = str(order.get("id") or "")
    title = order.get("title") or f"Order {source_id}"
    total = order.get("total", 0)
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=json.dumps(order.get("order_items") or [], default=str),
        source="keap.orders",
        connector_id=connector_id,
        tenant_id=tenant_id,
        metadata={
            "total": total,
            "status": order.get("status", ""),
            "contact_id": (order.get("contact") or {}).get("id"),
        },
    )
