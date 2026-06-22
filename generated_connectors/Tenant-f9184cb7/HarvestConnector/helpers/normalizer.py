"""Normalize Harvest API resources into NormalizedDocument."""
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


def normalize_time_entry(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Harvest /time_entries record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    entry = raw or {}
    source_id = str(entry.get("id", "") or "")
    spent_date = entry.get("spent_date", "")
    project = entry.get("project") or {}
    task = entry.get("task") or {}
    user = entry.get("user") or {}
    hours = entry.get("hours", 0.0)
    notes = entry.get("notes") or ""

    title = (
        f"Time entry — {project.get('name', 'Project')} "
        f"/ {task.get('name', 'Task')} ({spent_date})"
    )
    content_parts = [
        f"Date: {spent_date}",
        f"Hours: {hours}",
        f"Project: {project.get('name', '')}",
        f"Task: {task.get('name', '')}",
        f"User: {user.get('name', '')}",
    ]
    if notes:
        content_parts.append(f"Notes: {notes}")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        content_type="text",
        author=user.get("name"),
        created_at=_parse_dt(entry.get("created_at")),
        updated_at=_parse_dt(entry.get("updated_at")),
        metadata={
            "spent_date": spent_date,
            "hours": hours,
            "project_id": project.get("id"),
            "project_name": project.get("name", ""),
            "task_id": task.get("id"),
            "task_name": task.get("name", ""),
            "user_id": user.get("id"),
            "is_billed": entry.get("is_billed", False),
            "is_locked": entry.get("is_locked", False),
            "kind": "harvest.time_entry",
        },
        source="harvest.time_entries",
        connector_id=connector_id,
        tenant_id=tenant_id,
    )


def normalize_invoice(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Harvest /invoices record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    inv = raw or {}
    source_id = str(inv.get("id", "") or "")
    number = inv.get("number", "")
    client = inv.get("client") or {}
    state = inv.get("state", "")
    amount = inv.get("amount", 0.0)
    currency = inv.get("currency", "USD")

    title = f"Invoice {number}" if number else f"Invoice {source_id}"
    content = "\n".join(
        [
            f"Number: {number}",
            f"State: {state}",
            f"Amount: {amount} {currency}",
            f"Client: {client.get('name', '')}",
            f"Issue date: {inv.get('issue_date', '')}",
            f"Due date: {inv.get('due_date', '')}",
        ]
    )
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        author=client.get("name"),
        created_at=_parse_dt(inv.get("created_at")),
        updated_at=_parse_dt(inv.get("updated_at")),
        metadata={
            "number": number,
            "state": state,
            "amount": amount,
            "currency": currency,
            "due_date": inv.get("due_date"),
            "paid_date": inv.get("paid_date"),
            "client_id": client.get("id"),
            "client_name": client.get("name", ""),
            "kind": "harvest.invoice",
        },
        source="harvest.invoices",
        connector_id=connector_id,
        tenant_id=tenant_id,
    )


def normalize_client(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Harvest /clients record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    c = raw or {}
    source_id = str(c.get("id", "") or "")
    name = c.get("name", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Client {source_id}",
        content="\n".join(
            [
                f"Name: {name}",
                f"Currency: {c.get('currency', '')}",
                f"Address: {c.get('address', '')}",
                f"Active: {c.get('is_active', True)}",
            ]
        ),
        content_type="text",
        created_at=_parse_dt(c.get("created_at")),
        updated_at=_parse_dt(c.get("updated_at")),
        metadata={
            "currency": c.get("currency"),
            "is_active": c.get("is_active", True),
            "address": c.get("address", ""),
            "kind": "harvest.client",
        },
        source="harvest.clients",
        connector_id=connector_id,
        tenant_id=tenant_id,
    )
