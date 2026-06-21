"""Normalize Mercury REST resources into NormalizedDocument."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from helpers.utils import parse_dt


def normalize_account(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Mercury account into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id") or raw.get("accountId") or "")
    name = raw.get("name") or raw.get("nickname") or ""
    kind = raw.get("kind") or ""
    summary = (
        f"{name} ({kind})" if kind else name
    ) or f"Mercury account {source_id}"
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Mercury account {source_id}",
        content=summary,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=parse_dt(raw.get("createdAt")),
        updated_at=parse_dt(raw.get("updatedAt") or raw.get("postedAt")),
        metadata={
            "kind": kind,
            "status": raw.get("status", ""),
            "type": raw.get("type", ""),
            "availableBalance": raw.get("availableBalance"),
            "currentBalance": raw.get("currentBalance"),
            "routingNumber": raw.get("routingNumber"),
            "source": "mercury.account",
        },
    )


def normalize_transaction(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    account_id: str = "",
):
    """Turn a Mercury transaction into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id") or "")
    counterparty = raw.get("counterpartyName") or ""
    amount = raw.get("amount") or 0.0
    note = raw.get("note") or ""
    title = counterparty or note or f"Mercury txn {source_id}"
    content = (
        f"{raw.get('kind', '')} {amount}"
        + (f" → {counterparty}" if counterparty else "")
        + (f" — {note}" if note else "")
    ).strip()
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content or title,
        content_type="text",
        source_url=None,
        url=None,
        author=counterparty or None,
        created_at=parse_dt(raw.get("createdAt")),
        updated_at=parse_dt(raw.get("postedAt") or raw.get("createdAt")),
        metadata={
            "accountId": raw.get("accountId") or account_id,
            "amount": amount,
            "status": raw.get("status", ""),
            "kind": raw.get("kind", ""),
            "counterpartyName": counterparty,
            "counterpartyId": raw.get("counterpartyId", ""),
            "note": note,
            "externalMemo": raw.get("externalMemo", ""),
            "source": "mercury.transaction",
        },
    )


def normalize_recipient(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Mercury recipient into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id") or "")
    name = raw.get("name") or ""
    emails: List[str] = list(raw.get("emails") or [])
    pm = raw.get("defaultPaymentMethod") or ""
    content = name
    if emails:
        content += " · " + ", ".join(emails)
    if pm:
        content += f" · default={pm}"
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Mercury recipient {source_id}",
        content=content or name,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=parse_dt(raw.get("createdAt")),
        updated_at=parse_dt(raw.get("updatedAt")),
        metadata={
            "status": raw.get("status", ""),
            "defaultPaymentMethod": pm,
            "emails": emails,
            "paymentMethods": list(raw.get("paymentMethods") or []),
            "source": "mercury.recipient",
        },
    )


def normalize_statement(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
    account_id: str,
    start: str,
    end: str,
):
    """Turn a Mercury statement record into a NormalizedDocument.

    Mercury returns either `{statements: [...]}` or a raw list of statement
    items depending on endpoint version — both are tolerated.
    """
    from shared.base_connector import NormalizedDocument

    items: List[Dict[str, Any]]
    if isinstance(raw, dict):
        items = list(raw.get("statements") or raw.get("items") or [])
    elif isinstance(raw, list):
        items = list(raw)
    else:
        items = []
    source_id = f"{account_id}-{start}-{end}"
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=f"Statement {start} → {end}",
        content=json.dumps(items, default=str)[:4096],
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=parse_dt(start),
        updated_at=parse_dt(end),
        metadata={
            "accountId": account_id,
            "startDate": start,
            "endDate": end,
            "itemCount": len(items),
            "source": "mercury.statement",
        },
    )
