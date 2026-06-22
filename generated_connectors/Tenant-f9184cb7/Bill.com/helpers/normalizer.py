"""Normalize Bill.com API resources into NormalizedDocument.

The id convention follows the platform contract: ``f"{tenant_id}_{source_id}"``
so KB rows stay scoped per tenant even when source IDs collide across orgs.
"""
from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    """Best-effort parse of a Bill.com timestamp string into an aware datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            # Bill.com sends ``YYYY-MM-DD`` for dates and ISO8601 for datetimes.
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_vendor(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Bill.com vendor record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id", ""))
    name = raw.get("name", "") or ""
    email = raw.get("email", "") or ""
    address_parts = [
        raw.get("address1", ""),
        raw.get("addressCity", ""),
        raw.get("addressState", ""),
        raw.get("addressZip", ""),
        raw.get("addressCountry", ""),
    ]
    address = ", ".join(p for p in address_parts if p)
    content = "\n".join(filter(None, [name, email, address]))
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Vendor {source_id}",
        content=content,
        content_type="text",
        author=email or None,
        created_at=_parse_dt(raw.get("createdTime")),
        updated_at=_parse_dt(raw.get("updatedTime")),
        metadata={
            "email": email,
            "address1": raw.get("address1", ""),
            "city": raw.get("addressCity", ""),
            "state": raw.get("addressState", ""),
            "zip": raw.get("addressZip", ""),
            "country": raw.get("addressCountry", ""),
            "is_active": raw.get("isActive", ""),
            "kind": "billcom.vendor",
        },
    )


def normalize_bill(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Bill.com bill into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id", ""))
    invoice_number = raw.get("invoiceNumber", "") or ""
    vendor_id = raw.get("vendorId", "") or ""
    line_items = raw.get("billLineItems", []) or []
    line_summary = ", ".join(
        f"{li.get('description', '') or li.get('chartOfAccountId', '')}: {li.get('amount', '')}"
        for li in line_items
        if isinstance(li, dict)
    )
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=f"Bill {invoice_number}" if invoice_number else f"Bill {source_id}",
        content=line_summary or str(raw.get("description", "") or ""),
        content_type="text",
        created_at=_parse_dt(raw.get("createdTime") or raw.get("invoiceDate")),
        updated_at=_parse_dt(raw.get("updatedTime")),
        metadata={
            "vendor_id": vendor_id,
            "invoice_number": invoice_number,
            "invoice_date": raw.get("invoiceDate", ""),
            "due_date": raw.get("dueDate", ""),
            "amount": raw.get("amount"),
            "payment_status": raw.get("paymentStatus", ""),
            "kind": "billcom.bill",
        },
    )


def normalize_customer(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Bill.com customer record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id", ""))
    name = raw.get("name", "") or ""
    email = raw.get("email", "") or ""
    bill_addr = raw.get("billAddress1", "") or ""
    content = "\n".join(filter(None, [name, email, bill_addr]))
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Customer {source_id}",
        content=content,
        content_type="text",
        author=email or None,
        created_at=_parse_dt(raw.get("createdTime")),
        updated_at=_parse_dt(raw.get("updatedTime")),
        metadata={
            "email": email,
            "bill_address1": bill_addr,
            "kind": "billcom.customer",
        },
    )


def normalize_invoice(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Bill.com invoice into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id", ""))
    invoice_number = raw.get("invoiceNumber", "") or ""
    customer_id = raw.get("customerId", "") or ""
    line_items = raw.get("invoiceLineItems", []) or []
    line_summary = ", ".join(
        f"{li.get('description', '') or li.get('itemId', '')}: {li.get('amount', '')}"
        for li in line_items
        if isinstance(li, dict)
    )
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=f"Invoice {invoice_number}" if invoice_number else f"Invoice {source_id}",
        content=line_summary or str(raw.get("description", "") or ""),
        content_type="text",
        created_at=_parse_dt(raw.get("createdTime") or raw.get("invoiceDate")),
        updated_at=_parse_dt(raw.get("updatedTime")),
        metadata={
            "customer_id": customer_id,
            "invoice_number": invoice_number,
            "invoice_date": raw.get("invoiceDate", ""),
            "due_date": raw.get("dueDate", ""),
            "amount": raw.get("amount"),
            "kind": "billcom.invoice",
        },
    )
