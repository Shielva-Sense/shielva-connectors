"""Normalize AWS S3 resources into NormalizedDocument.

`connector.py` orchestrates; this module owns the wire→canonical mapping.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_object(
    raw: Dict[str, Any],
    bucket: str,
    connector_id: str,
    tenant_id: str,
    *,
    content_type: Optional[str] = None,
):
    """Turn an S3 `Contents`/`HeadObject` payload into a NormalizedDocument.

    The body is NOT eagerly fetched — `content` is left empty so callers can
    decide whether to retrieve it via `get_object` (or skip it for the
    KB-index-of-files use case).
    """
    from shared.base_connector import NormalizedDocument

    key = raw.get("Key") or raw.get("key") or ""
    last_mod = raw.get("LastModified") or raw.get("last_modified")
    etag = str(raw.get("ETag") or raw.get("etag") or "").strip('"')
    size = int(raw.get("Size") or raw.get("size") or 0)
    storage_class = raw.get("StorageClass") or raw.get("storage_class") or ""
    version_id = raw.get("VersionId") or raw.get("version_id")
    content_t = (
        content_type
        or raw.get("ContentType")
        or raw.get("content_type")
        or "application/octet-stream"
    )

    title = key.rsplit("/", 1)[-1] if key else bucket
    source_id = f"{bucket}/{key}" if key else bucket
    parsed = _parse_dt(last_mod)

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content="",
        content_type="text" if content_t.startswith("text/") else "binary",
        source_url=f"s3://{bucket}/{key}" if key else f"s3://{bucket}",
        url=None,
        author=None,
        created_at=parsed,
        updated_at=parsed,
        metadata={
            "bucket": bucket,
            "key": key,
            "size": size,
            "etag": etag,
            "storage_class": storage_class,
            "version_id": version_id,
            "object_content_type": content_t,
            "kind": "aws_s3.object",
        },
    )


def normalize_bucket(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn an S3 bucket envelope from `ListBuckets` into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    name = raw.get("Name") or raw.get("name") or ""
    creation = raw.get("CreationDate") or raw.get("creation_date")
    parsed = _parse_dt(creation)
    source_id = f"{name}" if name else "(unnamed)"

    return NormalizedDocument(
        id=f"{tenant_id}_bucket_{source_id}",
        source_id=source_id,
        title=name or "(unnamed)",
        content="",
        content_type="binary",
        source_url=f"s3://{name}" if name else None,
        url=None,
        author=None,
        created_at=parsed,
        updated_at=parsed,
        metadata={
            "bucket": name,
            "kind": "aws_s3.bucket",
        },
    )
