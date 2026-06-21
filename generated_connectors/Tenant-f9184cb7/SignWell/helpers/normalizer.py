"""Normalize SignWell API resources into NormalizedDocument."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.base_connector import NormalizedDocument


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse a SignWell RFC 3339 / ISO 8601 timestamp. Returns None on failure."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def normalize_document(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Project a SignWell document API response into a Shielva NormalizedDocument.

    Tenant-scoped id is mandatory — `id = f"{tenant_id}_{source_id}"`.
    """
    source_id = str(raw.get("id", ""))
    name = raw.get("name") or raw.get("subject") or "Untitled document"
    status = raw.get("status", "unknown")

    recipients = raw.get("recipients", []) or []
    parts = []
    for r in recipients:
        email = r.get("email", "")
        rname = r.get("name", "")
        rstatus = r.get("status", "")
        parts.append(f"{rname} <{email}> — {rstatus}")
    content = (
        "Recipients:\n" + "\n".join(parts)
        if parts
        else f"Document status: {status}"
    )

    author_email: Optional[str] = None
    if recipients and isinstance(recipients[0], dict):
        author_email = recipients[0].get("email")

    files = raw.get("files") or []
    source_url: Optional[str] = None
    if files and isinstance(files[0], dict):
        source_url = files[0].get("url")

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=content,
        content_type="text",
        source_url=source_url,
        author=author_email,
        created_at=_parse_iso(raw.get("created_at")) or datetime.now(timezone.utc),
        updated_at=_parse_iso(raw.get("updated_at")),
        metadata={
            "status": status,
            "test_mode": bool(raw.get("test_mode", False)),
            "embedded_signing": bool(raw.get("embedded_signing", False)),
            "recipients_count": len(recipients),
            "kind": "signwell.document",
        },
        source="signwell",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_template(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Project a SignWell template into a NormalizedDocument."""
    source_id = str(raw.get("id", ""))
    name = raw.get("name") or "Untitled template"
    description = raw.get("description") or ""
    fields = raw.get("fields") or []

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=description,
        content_type="text",
        created_at=_parse_iso(raw.get("created_at")) or datetime.now(timezone.utc),
        updated_at=_parse_iso(raw.get("updated_at")),
        metadata={
            "fields_count": len(fields),
            "kind": "signwell.template",
        },
        source="signwell",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
