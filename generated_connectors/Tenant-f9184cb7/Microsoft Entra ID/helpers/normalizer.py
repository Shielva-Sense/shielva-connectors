"""Normalize Microsoft Graph resources into NormalizedDocument.

Graph payloads are camelCase; we keep `metadata` keys camelCase to match the
upstream wire format so downstream search/filter on the KB matches the docs.
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


def normalize_user(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Microsoft Graph /users object into a NormalizedDocument.

    ``tenant_id`` is the **Shielva** tenant id (used to scope the doc id) — NOT
    the Azure tenant id (which lives in connector config).
    """
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id", "") or "")
    upn = str(raw.get("userPrincipalName", "") or "")
    display_name = str(raw.get("displayName", "") or upn or source_id)
    mail = str(raw.get("mail", "") or "")
    content_parts = [p for p in (upn, mail, display_name) if p]
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=display_name,
        content=" ".join(content_parts),
        content_type="text",
        author=upn or None,
        created_at=_parse_dt(raw.get("createdDateTime")),
        updated_at=_parse_dt(raw.get("createdDateTime")),
        metadata={
            "userPrincipalName": upn,
            "mail": mail,
            "accountEnabled": bool(raw.get("accountEnabled", True)),
            "jobTitle": raw.get("jobTitle", ""),
            "department": raw.get("department", ""),
            "userType": raw.get("userType", ""),
            "kind": "entra_id.user",
        },
    )


def normalize_group(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Microsoft Graph /groups object into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = str(raw.get("id", "") or "")
    display_name = str(raw.get("displayName", "") or source_id)
    description = str(raw.get("description", "") or "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=display_name,
        content=description or display_name,
        content_type="text",
        created_at=_parse_dt(raw.get("createdDateTime")),
        updated_at=_parse_dt(raw.get("createdDateTime")),
        metadata={
            "mailNickname": raw.get("mailNickname", ""),
            "mailEnabled": bool(raw.get("mailEnabled", False)),
            "securityEnabled": bool(raw.get("securityEnabled", True)),
            "groupTypes": raw.get("groupTypes", []) or [],
            "kind": "entra_id.group",
        },
    )
