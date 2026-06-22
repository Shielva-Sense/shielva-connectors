"""Normalize Firebase resources into NormalizedDocument.

Single owner of the Firestore Value decode + Identity Toolkit user mapping.
"""
from datetime import datetime, timezone
from typing import Any, Dict

from shared.base_connector import NormalizedDocument

from helpers.utils import epoch_ms_to_datetime, parse_rfc3339


# ── Firestore Value decoding ────────────────────────────────────────────────


def _decode_firestore_value(value: Dict[str, Any]) -> Any:
    """Decode a single Firestore REST `Value` envelope into a Python value."""
    if not isinstance(value, dict):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        try:
            return int(value["integerValue"])
        except (TypeError, ValueError):
            return value["integerValue"]
    if "doubleValue" in value:
        return value["doubleValue"]
    if "booleanValue" in value:
        return value["booleanValue"]
    if "nullValue" in value:
        return None
    if "timestampValue" in value:
        return value["timestampValue"]
    if "bytesValue" in value:
        return value["bytesValue"]
    if "referenceValue" in value:
        return value["referenceValue"]
    if "geoPointValue" in value:
        return value["geoPointValue"]
    if "arrayValue" in value:
        return [
            _decode_firestore_value(v)
            for v in (value["arrayValue"] or {}).get("values", []) or []
        ]
    if "mapValue" in value:
        return {
            k: _decode_firestore_value(v)
            for k, v in ((value["mapValue"] or {}).get("fields", {}) or {}).items()
        }
    return value


def decode_firestore_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _decode_firestore_value(v) for k, v in (fields or {}).items()}


# ── Firestore document → NormalizedDocument ────────────────────────────────


def normalize_firestore_document(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str,
    *,
    collection: str = "",
) -> NormalizedDocument:
    """Convert a Firestore REST document into a NormalizedDocument.

    Firestore documents are shaped:
        {"name": "projects/.../documents/coll/id",
         "fields": {...},
         "createTime": "...",
         "updateTime": "..."}
    """
    name: str = raw.get("name", "") or ""
    doc_id = name.rsplit("/", 1)[-1] if name else (raw.get("id", "") or "")
    decoded = decode_firestore_fields(raw.get("fields", {}))

    created_at = parse_rfc3339(raw.get("createTime"))
    updated_at = parse_rfc3339(raw.get("updateTime")) or created_at

    title_value = decoded.get("title") or decoded.get("name") or doc_id or "Firestore document"
    title = str(title_value)

    return NormalizedDocument(
        id=f"{tenant_id}_{doc_id}" if doc_id else f"{tenant_id}_firestore_doc",
        source_id=doc_id,
        title=title,
        content=str(decoded),
        content_type="text",
        source="firebase.firestore",
        tenant_id=tenant_id,
        connector_id=connector_id,
        created_at=created_at,
        updated_at=updated_at,
        metadata={
            "firestore_name": name,
            "fields": decoded,
            "collection": collection or _collection_from_name(name),
            "kind": "firebase.firestore",
        },
    )


def _collection_from_name(name: str) -> str:
    # name = projects/{p}/databases/(default)/documents/{collection}/{id}
    if not name:
        return ""
    parts = name.split("/documents/", 1)
    if len(parts) != 2:
        return ""
    tail = parts[1].split("/")
    return tail[0] if tail else ""


# ── Identity Toolkit user → NormalizedDocument ─────────────────────────────


def normalize_auth_user(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Convert an Identity Toolkit user record into a NormalizedDocument."""
    uid = str(raw.get("localId") or raw.get("uid") or "")
    email = raw.get("email") or ""
    display_name = raw.get("displayName") or ""
    title = display_name or email or uid or "Firebase user"

    created_at = epoch_ms_to_datetime(raw.get("createdAt"))
    updated_at = epoch_ms_to_datetime(raw.get("lastLoginAt")) or created_at

    return NormalizedDocument(
        id=f"{tenant_id}_{uid}" if uid else f"{tenant_id}_firebase_user",
        source_id=uid,
        title=str(title),
        content=(
            f"email={email} verified={raw.get('emailVerified', False)} "
            f"disabled={raw.get('disabled', False)}"
        ),
        content_type="text",
        source="firebase.auth",
        tenant_id=tenant_id,
        connector_id=connector_id,
        created_at=created_at or datetime.now(timezone.utc),
        updated_at=updated_at or datetime.now(timezone.utc),
        metadata={
            "email": email,
            "email_verified": raw.get("emailVerified", False),
            "disabled": raw.get("disabled", False),
            "provider_user_info": raw.get("providerUserInfo", []),
            "kind": "firebase.auth_user",
        },
    )
