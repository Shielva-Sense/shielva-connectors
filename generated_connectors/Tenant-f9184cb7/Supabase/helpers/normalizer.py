"""Normalize Supabase REST rows, Auth users, and Storage objects into NormalizedDocument."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse a Supabase timestamp (ISO-8601 / RFC 3339) into an aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _row_source_id(table: str, row: Dict[str, Any]) -> str:
    """Stable per-row source id — uses `id` if present, else SHA-256 of row body."""
    if isinstance(row, dict) and row.get("id") is not None:
        return f"{table}:{row['id']}"
    payload = json.dumps(row, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"{table}:{digest}"


def normalize_row(
    table: str,
    row: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Supabase PostgREST row into a NormalizedDocument.

    The id follows the multi-tenant pattern `f"{tenant_id}_{source_id}"`.
    """
    from shared.base_connector import NormalizedDocument

    source_id = _row_source_id(table, row)
    title = (
        row.get("title")
        or row.get("name")
        or row.get("subject")
        or source_id
    )
    content_value = (
        row.get("content")
        or row.get("body")
        or row.get("description")
        or ""
    )
    if not content_value:
        content_value = json.dumps(row, default=str)
    if not isinstance(content_value, str):
        content_value = json.dumps(content_value, default=str)

    created_at = _parse_dt(row.get("created_at") or row.get("inserted_at"))
    updated_at = _parse_dt(row.get("updated_at")) or created_at

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=str(title),
        content=content_value,
        content_type="text",
        source_url=None,
        url=None,
        author=str(row.get("author") or row.get("user_id") or "") or None,
        created_at=created_at,
        updated_at=updated_at,
        source="supabase",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={"table": table, "raw": row, "kind": "supabase.row"},
    )


def normalize_user(
    user: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Supabase Auth (GoTrue) user record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    user_id = user.get("id") or user.get("user_id") or ""
    source_id = f"users:{user_id}"
    email = user.get("email") or user.get("phone") or source_id
    user_metadata = user.get("user_metadata") or {}
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=str(email),
        content=json.dumps(user_metadata, default=str),
        content_type="text",
        author=str(email),
        created_at=_parse_dt(user.get("created_at")),
        updated_at=_parse_dt(user.get("updated_at")),
        source="supabase",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "role": user.get("role"),
            "aud": user.get("aud"),
            "user_metadata": user_metadata,
            "app_metadata": user.get("app_metadata") or {},
            "kind": "supabase.user",
        },
    )


def normalize_storage_object(
    bucket: str,
    obj: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Supabase Storage object metadata record into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    path = obj.get("name") or obj.get("path") or ""
    source_id = f"{bucket}/{path}"
    meta = obj.get("metadata") or {}
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=path or source_id,
        content="",
        content_type=meta.get("mimetype") or "application/octet-stream",
        source_url=obj.get("signedURL") or obj.get("signed_url"),
        author=None,
        created_at=None,
        updated_at=None,
        source="supabase",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "bucket": bucket,
            "path": path,
            "size": meta.get("size"),
            "mimetype": meta.get("mimetype"),
            "kind": "supabase.storage_object",
        },
    )
