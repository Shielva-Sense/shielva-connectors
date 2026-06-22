"""Normalize Adobe Sign API resources into ``NormalizedDocument``."""
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


def _participant_emails(raw: Dict[str, Any]) -> List[str]:
    """Pull a flat list of participant emails from an agreement payload.

    Adobe Sign v6 returns participants under two shapes:
      • ``participantSetsInfo[*].memberInfos[*].email`` (create / get)
      • ``displayUserSetInfos[*].displayUserSetMemberInfos[*].email`` (list)
    """
    emails: List[str] = []
    for ps in raw.get("participantSetsInfo", []) or []:
        for m in (ps.get("memberInfos") or []):
            email = m.get("email")
            if email and email not in emails:
                emails.append(email)
    for ds in raw.get("displayUserSetInfos", []) or []:
        for m in (ds.get("displayUserSetMemberInfos") or []):
            email = m.get("email")
            if email and email not in emails:
                emails.append(email)
    return emails


def normalize_agreement(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn an Adobe Sign agreement into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    agreement = raw.get("agreement", raw) if isinstance(raw, dict) else {}
    source_id = str(agreement.get("id", "") or "")
    name = agreement.get("name") or "(unnamed agreement)"
    status = agreement.get("status", "UNKNOWN")
    participants = _participant_emails(agreement)
    message = agreement.get("message", "") or ""
    content_lines = [
        f"Agreement: {name}",
        f"Status: {status}",
    ]
    created_date = agreement.get("createdDate")
    if created_date:
        content_lines.append(f"Created: {created_date}")
    if participants:
        content_lines.append("Participants: " + ", ".join(participants))
    if message:
        content_lines.append(f"Message: {message}")

    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=str(name),
        content="\n".join(content_lines),
        content_type="text",
        source="adobe_sign.agreement",
        author=participants[0] if participants else agreement.get("senderEmail"),
        created_at=_parse_dt(created_date),
        updated_at=_parse_dt(agreement.get("expirationTime") or created_date),
        metadata={
            "status": status,
            "type": agreement.get("type"),
            "groupId": agreement.get("groupId"),
            "expirationTime": agreement.get("expirationTime"),
            "senderEmail": agreement.get("senderEmail"),
            "participants": participants,
            "kind": "adobe_sign.agreement",
        },
        connector_id=connector_id,
        tenant_id=tenant_id,
    )


def normalize_agreements_page(
    raw_page: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> List[Any]:
    """Normalize a full list_agreements() response into NormalizedDocuments."""
    agreements = (
        raw_page.get("userAgreementList")
        or raw_page.get("agreementList")
        or raw_page.get("agreements")
        or []
    )
    return [normalize_agreement(a, connector_id, tenant_id) for a in agreements]


def normalize_library_document(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn an Adobe Sign library document (reusable template) into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    doc = raw.get("libraryDocument", raw) if isinstance(raw, dict) else {}
    source_id = str(doc.get("id", "") or "")
    name = doc.get("name") or "(unnamed library document)"
    scope = doc.get("scope", "")
    content_lines = [
        f"Library Document: {name}",
        f"Scope: {scope}",
    ]
    template_types = doc.get("templateTypes") or []
    if template_types:
        content_lines.append("Types: " + ", ".join(template_types))

    return NormalizedDocument(
        id=f"{connector_id}_{source_id}",
        source_id=source_id,
        title=str(name),
        content="\n".join(content_lines),
        content_type="text",
        source="adobe_sign.libraryDocument",
        author=None,
        created_at=_parse_dt(doc.get("modifiedDate") or doc.get("createdDate")),
        updated_at=_parse_dt(doc.get("modifiedDate")),
        metadata={
            "scope": scope,
            "templateTypes": template_types,
            "status": doc.get("status"),
            "kind": "adobe_sign.libraryDocument",
        },
        connector_id=connector_id,
        tenant_id=tenant_id,
    )
