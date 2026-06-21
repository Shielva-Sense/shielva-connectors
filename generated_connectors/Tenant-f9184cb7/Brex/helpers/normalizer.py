"""Normalize Brex API resources into Shielva NormalizedDocument records."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.base_connector import NormalizedDocument


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # Brex emits RFC 3339 strings, sometimes with a trailing Z.
        v = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(v)
    except Exception:
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_user(
    raw: Dict[str, Any], connector_id: str, tenant_id: str,
) -> NormalizedDocument:
    """Convert a Brex user object to a NormalizedDocument."""
    user_id = raw.get("id", "")
    first = raw.get("first_name") or ""
    last = raw.get("last_name") or ""
    email = raw.get("email") or ""
    title = f"{first} {last}".strip() or email or f"Brex user {user_id}"
    parts = []
    if email:
        parts.append(f"Email: {email}")
    status = raw.get("status")
    if status:
        parts.append(f"Status: {status}")
    role = raw.get("role")
    if role:
        parts.append(f"Role: {role}")
    return NormalizedDocument(
        id=f"{tenant_id}_{user_id}",
        source_id=user_id,
        title=title,
        content="\n".join(parts) or title,
        content_type="text",
        author=email or None,
        created_at=_parse_dt(raw.get("created_at")),
        updated_at=_parse_dt(raw.get("updated_at")),
        metadata={
            "email": email,
            "status": status,
            "role": role,
            "department_id": raw.get("department_id"),
            "location_id": raw.get("location_id"),
            "kind": "brex.user",
        },
        source="brex.users",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_transaction(
    raw: Dict[str, Any], connector_id: str, tenant_id: str,
) -> NormalizedDocument:
    """Convert a Brex card transaction to a NormalizedDocument."""
    tx_id = raw.get("id", "")
    description = raw.get("description") or ""
    amount = raw.get("amount") or {}
    cents = amount.get("amount") if isinstance(amount, dict) else None
    currency = amount.get("currency") if isinstance(amount, dict) else ""
    merchant = (raw.get("merchant") or {}).get("raw_descriptor") or ""

    title = description or merchant or f"Brex transaction {tx_id}"
    parts = []
    if description:
        parts.append(description)
    if merchant:
        parts.append(f"Merchant: {merchant}")
    if cents is not None and currency:
        parts.append(f"Amount: {cents / 100:.2f} {currency}")
    return NormalizedDocument(
        id=f"{tenant_id}_{tx_id}",
        source_id=tx_id,
        title=title,
        content="\n".join(parts) or title,
        content_type="text",
        created_at=_parse_dt(raw.get("posted_at_date") or raw.get("initiated_at_date")),
        updated_at=_parse_dt(raw.get("posted_at_date")),
        metadata={
            "amount_cents": cents,
            "currency": currency,
            "type": raw.get("type"),
            "card_id": raw.get("card_id"),
            "merchant_category_code": (raw.get("merchant") or {}).get("mcc"),
            "kind": "brex.transaction",
        },
        source="brex.transactions",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_expense(
    raw: Dict[str, Any], connector_id: str, tenant_id: str,
) -> NormalizedDocument:
    """Convert a Brex expense to a NormalizedDocument."""
    expense_id = raw.get("id", "")
    memo = raw.get("memo") or ""
    category = raw.get("category") or ""
    amount = raw.get("amount") or {}
    cents = amount.get("amount") if isinstance(amount, dict) else None
    currency = amount.get("currency") if isinstance(amount, dict) else ""
    merchant = (raw.get("merchant") or {}).get("raw_descriptor") or ""

    title = memo or f"Brex expense {expense_id}"
    parts = []
    if merchant:
        parts.append(f"Merchant: {merchant}")
    if cents is not None and currency:
        parts.append(f"Amount: {cents / 100:.2f} {currency}")
    if category:
        parts.append(f"Category: {category}")
    if memo:
        parts.append(memo)
    content = "\n".join(parts) or memo or title
    return NormalizedDocument(
        id=f"{tenant_id}_{expense_id}",
        source_id=expense_id,
        title=title,
        content=content,
        content_type="text",
        author=raw.get("user_id"),
        created_at=_parse_dt(raw.get("purchased_at") or raw.get("created_at")),
        updated_at=_parse_dt(raw.get("updated_at")),
        metadata={
            "amount_cents": cents,
            "currency": currency,
            "category": category,
            "status": raw.get("status"),
            "payment_status": raw.get("payment_status"),
            "expense_type": raw.get("expense_type"),
            "kind": "brex.expense",
        },
        source="brex.expenses",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
