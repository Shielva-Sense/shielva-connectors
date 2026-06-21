"""Normalize Ramp API resources into NormalizedDocument."""
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


def normalize_transaction(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Ramp transaction into a NormalizedDocument.

    Document id is `f"{tenant_id}_{source_id}"` per Shielva SOC contract.
    """
    from shared.base_connector import NormalizedDocument

    tx = raw if isinstance(raw, dict) else {}
    source_id = tx.get("id", "")
    merchant = tx.get("merchant_name", "") or tx.get("merchant_descriptor", "")
    amount = tx.get("amount", 0)
    currency = tx.get("currency_code", "USD")
    title = f"{merchant} ({currency} {amount})" if merchant else f"Transaction {source_id}"
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=str(tx.get("memo", "") or tx.get("sk_category_name", "")),
        content_type="text",
        author=tx.get("card_holder", {}).get("user_id") if isinstance(tx.get("card_holder"), dict) else None,
        created_at=_parse_dt(tx.get("user_transaction_time")),
        updated_at=_parse_dt(tx.get("user_transaction_time")),
        metadata={
            "amount": amount,
            "currency_code": currency,
            "merchant_name": merchant,
            "card_id": tx.get("card_id"),
            "user_id": tx.get("user_id"),
            "sk_category_id": tx.get("sk_category_id"),
            "sk_category_name": tx.get("sk_category_name"),
            "kind": "ramp.transaction",
        },
    )


def normalize_user(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Ramp user into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    u = raw if isinstance(raw, dict) else {}
    source_id = u.get("id", "")
    first = u.get("first_name", "")
    last = u.get("last_name", "")
    full_name = " ".join(p for p in (first, last) if p) or u.get("email", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=full_name,
        content=u.get("email", ""),
        content_type="text",
        author=u.get("email"),
        created_at=_parse_dt(u.get("created_at")),
        updated_at=_parse_dt(u.get("updated_at")),
        metadata={
            "role": u.get("role"),
            "email": u.get("email"),
            "department_id": u.get("department_id"),
            "location_id": u.get("location_id"),
            "status": u.get("status"),
            "kind": "ramp.user",
        },
    )


def normalize_card(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Ramp card into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    c = raw if isinstance(raw, dict) else {}
    source_id = c.get("id", "")
    name = c.get("display_name", "") or f"Card {source_id}"
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=str(c.get("state", "")),
        content_type="text",
        created_at=_parse_dt(c.get("created_at")),
        updated_at=_parse_dt(c.get("updated_at")),
        metadata={
            "user_id": c.get("user_id"),
            "is_physical": c.get("is_physical"),
            "state": c.get("state"),
            "spending_restrictions": c.get("spending_restrictions"),
            "kind": "ramp.card",
        },
    )
