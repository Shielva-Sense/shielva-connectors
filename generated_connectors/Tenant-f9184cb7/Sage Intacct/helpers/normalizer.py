"""Normalize Sage Intacct rows into ``NormalizedDocument``.

Multi-tenant: every ``NormalizedDocument`` id has the form
``f"{tenant_id}_{source_id}"`` so two tenants with the same Intacct key
produce distinct documents in the Shielva knowledge base.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    """Best-effort parse of an Intacct timestamp string.

    Intacct emits dates in multiple shapes (``MM/DD/YYYY HH:MM:SS``,
    ``YYYY-MM-DDTHH:MM:SS``, ``YYYY-MM-DD``). Fall back to UTC now when
    nothing matches — never raise.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return datetime.now(timezone.utc)
    text = value.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _flat_content(row: Dict[str, Any]) -> str:
    """Render a row as a deterministic ``key: value`` block for full-text search."""
    parts = []
    for k in sorted(row.keys()):
        v = row[k]
        if v is None or v == "":
            continue
        if isinstance(v, dict):
            v = ", ".join(f"{ik}={iv}" for ik, iv in v.items() if iv)
        parts.append(f"{k}: {v}")
    return "\n".join(parts)


def normalize_customer(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Map a CUSTOMER row to a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("CUSTOMERID") or raw.get("RECORDNO") or "")
    name = str(raw.get("NAME") or source_id or "Customer")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}" if source_id else f"{tenant_id}_customer",
        source_id=source_id,
        title=name,
        content=_flat_content(raw),
        content_type="text",
        source="sage_intacct.customer",
        source_url=None,
        url=None,
        author=str(raw.get("CREATEDBY") or "") or None,
        created_at=_parse_dt(raw.get("WHENCREATED")),
        updated_at=_parse_dt(raw.get("WHENMODIFIED") or raw.get("WHENCREATED")),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "object": "CUSTOMER",
            "customerid": raw.get("CUSTOMERID", ""),
            "status": raw.get("STATUS", ""),
            "currency": raw.get("CURRENCY", ""),
            **raw,
        },
    )


def normalize_vendor(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Map a VENDOR row to a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("VENDORID") or raw.get("RECORDNO") or "")
    name = str(raw.get("NAME") or source_id or "Vendor")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}" if source_id else f"{tenant_id}_vendor",
        source_id=source_id,
        title=name,
        content=_flat_content(raw),
        content_type="text",
        source="sage_intacct.vendor",
        source_url=None,
        url=None,
        author=str(raw.get("CREATEDBY") or "") or None,
        created_at=_parse_dt(raw.get("WHENCREATED")),
        updated_at=_parse_dt(raw.get("WHENMODIFIED") or raw.get("WHENCREATED")),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "object": "VENDOR",
            "vendorid": raw.get("VENDORID", ""),
            "status": raw.get("STATUS", ""),
            "currency": raw.get("CURRENCY", ""),
            **raw,
        },
    )


def normalize_gl_account(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Map a GLACCOUNT row to a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("ACCOUNTNO") or raw.get("RECORDNO") or "")
    title = str(raw.get("TITLE") or source_id or "GL Account")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}" if source_id else f"{tenant_id}_glaccount",
        source_id=source_id,
        title=title,
        content=_flat_content(raw),
        content_type="text",
        source="sage_intacct.glaccount",
        source_url=None,
        url=None,
        author=str(raw.get("CREATEDBY") or "") or None,
        created_at=_parse_dt(raw.get("WHENCREATED")),
        updated_at=_parse_dt(raw.get("WHENMODIFIED") or raw.get("WHENCREATED")),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "object": "GLACCOUNT",
            "accountno": raw.get("ACCOUNTNO", ""),
            "accounttype": raw.get("ACCOUNTTYPE", ""),
            "status": raw.get("STATUS", ""),
            **raw,
        },
    )


def normalize_row(
    object_name: str,
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Dispatch to the right normalizer for the given Intacct object name.

    Falls back to a generic NormalizedDocument when the object is not one of
    the three known catalog types — this keeps the sync loop monomorphic.
    """
    upper = (object_name or "").upper()
    if upper == "CUSTOMER":
        return normalize_customer(raw, connector_id, tenant_id)
    if upper == "VENDOR":
        return normalize_vendor(raw, connector_id, tenant_id)
    if upper == "GLACCOUNT":
        return normalize_gl_account(raw, connector_id, tenant_id)

    from shared.base_connector import NormalizedDocument

    source_id = str(
        raw.get("RECORDNO")
        or raw.get("CUSTOMERID")
        or raw.get("VENDORID")
        or raw.get("ACCOUNTNO")
        or ""
    )
    title = str(raw.get("NAME") or raw.get("TITLE") or f"{upper} {source_id}")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}" if source_id else f"{tenant_id}_{upper.lower()}",
        source_id=source_id,
        title=title,
        content=_flat_content(raw),
        content_type="text",
        source=f"sage_intacct.{upper.lower()}",
        source_url=None,
        url=None,
        author=str(raw.get("CREATEDBY") or "") or None,
        created_at=_parse_dt(raw.get("WHENCREATED")),
        updated_at=_parse_dt(raw.get("WHENMODIFIED") or raw.get("WHENCREATED")),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={"object": upper, **raw},
    )
