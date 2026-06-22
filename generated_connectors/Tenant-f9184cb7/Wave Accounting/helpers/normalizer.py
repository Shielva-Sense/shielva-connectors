"""Normalize Wave GraphQL resources into NormalizedDocument.

Lives outside connector.py per SOC: the orchestrator owns flow, the normalizer
owns wire-format → platform-shape translation.
"""
from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    """Parse a Wave ISO-8601 timestamp (or pass through a datetime) → UTC."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_customer(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Wave customer node into a NormalizedDocument.

    The id is tenant-scoped: `f"{tenant_id}_{customer.id}"`.
    """
    from shared.base_connector import NormalizedDocument

    customer = raw.get("node", raw) if isinstance(raw, dict) else {}
    source_id = customer.get("id", "")
    name = customer.get("name", "") or ""
    email = customer.get("email", "") or ""
    phone = customer.get("phone") or customer.get("mobile") or ""

    content_parts = [p for p in [name, email, phone] if p]
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or email or source_id,
        content=" — ".join(content_parts) if content_parts else "",
        content_type="text",
        source_url=None,
        url=None,
        author=email or None,
        created_at=_parse_dt(customer.get("createdAt")),
        updated_at=_parse_dt(customer.get("modifiedAt") or customer.get("updatedAt")),
        metadata={
            "email": email,
            "phone": phone,
            "first_name": customer.get("firstName", ""),
            "last_name": customer.get("lastName", ""),
            "kind": "wave.customer",
        },
    )


def normalize_invoice(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Wave invoice node into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    invoice = raw.get("node", raw) if isinstance(raw, dict) else {}
    source_id = invoice.get("id", "")
    number = invoice.get("invoiceNumber", "") or ""
    status = invoice.get("status", "") or ""
    total = invoice.get("total") or {}
    currency = (total.get("currency") or {}).get("code", "") or ""
    amount = total.get("value", "") or ""
    customer = invoice.get("customer") or {}

    title = f"Invoice {number}" if number else f"Invoice {source_id}"
    content = f"{status} — {amount} {currency}".strip(" —")

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        author=(customer.get("name") or "") if isinstance(customer, dict) else "",
        created_at=_parse_dt(invoice.get("invoiceDate") or invoice.get("createdAt")),
        updated_at=_parse_dt(invoice.get("modifiedAt") or invoice.get("updatedAt")),
        metadata={
            "number": number,
            "status": status,
            "total": amount,
            "currency": currency,
            "due_date": invoice.get("dueDate", ""),
            "customer_id": customer.get("id", "") if isinstance(customer, dict) else "",
            "kind": "wave.invoice",
        },
    )
