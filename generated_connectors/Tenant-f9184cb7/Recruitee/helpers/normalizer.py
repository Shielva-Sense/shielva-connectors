"""Normalize Recruitee API resources into NormalizedDocument."""
import re
from datetime import datetime, timezone
from typing import Any, Dict, List


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_addresses(items: List[Any]) -> List[str]:
    """Recruitee returns email/phone as list of {"normalized": str} or plain strings."""
    out: List[str] = []
    for item in items:
        if isinstance(item, str):
            if item:
                out.append(item)
        elif isinstance(item, dict):
            value = item.get("normalized") or item.get("address") or item.get("value")
            if value:
                out.append(str(value))
    return out


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def normalize_candidate(
    raw: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
):
    """Turn a Recruitee candidate dict into a NormalizedDocument.

    Per the system prompt: NormalizedDocument id = ``f"{tenant_id}_{source_id}"``.
    """
    from shared.base_connector import NormalizedDocument

    cand = raw.get("candidate", raw) if isinstance(raw, dict) else {}
    source_id = str(cand.get("id", ""))
    name = str(cand.get("name", "") or "")
    emails = _extract_addresses(_as_list(cand.get("emails")))
    phones = _extract_addresses(_as_list(cand.get("phones")))

    content_lines: List[str] = []
    if name:
        content_lines.append(name)
    if emails:
        content_lines.append("Emails: " + ", ".join(emails))
    if phones:
        content_lines.append("Phones: " + ", ".join(phones))
    content = "\n".join(content_lines)

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Candidate {source_id}",
        content=content,
        content_type="text",
        author=emails[0] if emails else None,
        created_at=_parse_dt(cand.get("created_at")),
        updated_at=_parse_dt(cand.get("updated_at")),
        source="recruitee.candidates",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "emails": emails,
            "phones": phones,
            "source": cand.get("source"),
            "photo_thumb_url": cand.get("photo_thumb_url"),
            "kind": "recruitee.candidate",
        },
    )


def normalize_offer(
    raw: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
):
    """Turn a Recruitee offer (job/requisition) dict into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    offer = raw.get("offer", raw) if isinstance(raw, dict) else {}
    source_id = str(offer.get("id", ""))
    title = str(offer.get("title", "") or "")
    description = _strip_tags(str(offer.get("description", "") or ""))
    requirements = _strip_tags(str(offer.get("requirements", "") or ""))

    content_parts: List[str] = []
    if description:
        content_parts.append(description)
    if requirements:
        content_parts.append("Requirements:\n" + requirements)
    content = "\n\n".join(content_parts) or title

    location_ids = [
        int(loc.get("id"))
        for loc in _as_list(offer.get("locations"))
        if isinstance(loc, dict) and loc.get("id") is not None
    ]

    department = offer.get("department")
    department_id = None
    if isinstance(department, dict):
        department_id = department.get("id")
    elif isinstance(department, int):
        department_id = department

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title or f"Offer {source_id}",
        content=content,
        content_type="text",
        created_at=_parse_dt(offer.get("created_at")),
        updated_at=_parse_dt(offer.get("updated_at")),
        source="recruitee.offers",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "status": offer.get("status"),
            "position_type": offer.get("position_type"),
            "employment_type_code": offer.get("employment_type_code"),
            "department_id": department_id,
            "location_ids": location_ids,
            "kind": "recruitee.offer",
        },
    )
