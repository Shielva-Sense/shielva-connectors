"""Normalize Dropbox Sign API resources into `NormalizedDocument`s.

`NormalizedDocument.id` is always tenant-scoped: `f"{tenant_id}_{source_id}"`.
This guarantees multi-tenant isolation when documents are ingested into a
shared KB.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List


def _parse_dt(value: Any) -> datetime:
    """Best-effort conversion of an API field to a UTC datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Dropbox Sign returns epoch seconds for `created_at`.
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_signature_request(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str = "",
):
    """Turn a Dropbox Sign `signature_request` payload into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    sr = raw.get("signature_request", raw) if isinstance(raw, dict) else {}
    if not isinstance(sr, dict):
        sr = {}
    source_id = sr.get("signature_request_id", "") or ""
    title = sr.get("title") or sr.get("subject") or f"Signature request {source_id}"
    content_parts: List[str] = []
    if sr.get("subject"):
        content_parts.append(f"Subject: {sr['subject']}")
    if sr.get("message"):
        content_parts.append(sr["message"])
    sigs = sr.get("signatures") or []
    if isinstance(sigs, list) and sigs:
        signer_lines = []
        for s in sigs:
            if isinstance(s, dict):
                name = s.get("signer_name") or s.get("signer_email_address") or ""
                status = s.get("status_code", "")
                signer_lines.append(f"- {name} ({status})")
        if signer_lines:
            content_parts.append("Signers:\n" + "\n".join(signer_lines))
    content = "\n\n".join(content_parts)

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        source_url=sr.get("details_url") or sr.get("signing_url"),
        url=sr.get("details_url") or sr.get("signing_url"),
        author=sr.get("requester_email_address"),
        created_at=_parse_dt(sr.get("created_at")),
        updated_at=_parse_dt(sr.get("created_at")),
        metadata={
            "is_complete": bool(sr.get("is_complete", False)),
            "is_declined": bool(sr.get("is_declined", False)),
            "has_error": bool(sr.get("has_error", False)),
            "requester_email_address": sr.get("requester_email_address", ""),
            "signing_url": sr.get("signing_url"),
            "details_url": sr.get("details_url"),
            "signatures": sigs if isinstance(sigs, list) else [],
            "kind": "dropbox_sign.signature_request",
        },
    )


def normalize_template(
    raw: Dict[str, Any],
    tenant_id: str,
    connector_id: str = "",
):
    """Turn a Dropbox Sign `template` payload into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    tpl = raw.get("template", raw) if isinstance(raw, dict) else {}
    if not isinstance(tpl, dict):
        tpl = {}
    source_id = tpl.get("template_id", "") or ""
    title = tpl.get("title") or f"Template {source_id}"

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=tpl.get("message", "") or "",
        content_type="text",
        author=None,
        created_at=_parse_dt(tpl.get("updated_at") or tpl.get("created_at")),
        updated_at=_parse_dt(tpl.get("updated_at") or tpl.get("created_at")),
        metadata={
            "can_edit": bool(tpl.get("can_edit", False)),
            "is_locked": bool(tpl.get("is_locked", False)),
            "signer_roles": tpl.get("signer_roles") or [],
            "kind": "dropbox_sign.template",
        },
    )


def extract_signature_requests(
    payload: Dict[str, Any],
    tenant_id: str,
    connector_id: str = "",
) -> List[Any]:
    """Pull the list of signature requests out of a `/signature_request/list` response."""
    items = payload.get("signature_requests", []) if isinstance(payload, dict) else []
    return [
        normalize_signature_request({"signature_request": item}, tenant_id, connector_id)
        for item in items
        if isinstance(item, dict)
    ]


def extract_templates(
    payload: Dict[str, Any],
    tenant_id: str,
    connector_id: str = "",
) -> List[Any]:
    """Pull the list of templates out of a `/template/list` response."""
    items = payload.get("templates", []) if isinstance(payload, dict) else []
    return [
        normalize_template({"template": item}, tenant_id, connector_id)
        for item in items
        if isinstance(item, dict)
    ]
