"""Transforms raw JazzHR API responses into `NormalizedDocument` objects.

All `NormalizedDocument.id` values are tenant-scoped via the tenant_id
prefix (per CONNECTOR_SYSTEM_PROMPT — never raw provider IDs alone).
"""
from datetime import datetime
from typing import Any, Dict, Optional

import structlog
from shared.base_connector import NormalizedDocument

logger = structlog.get_logger(__name__)

# JazzHR app URL prefix — used to populate NormalizedDocument.source_url.
_APP_BASE = "https://app.jazz.co/app/v2"


def _parse_date(value: Any) -> Optional[datetime]:
    """Parse a JazzHR-style date string into a datetime, returning None on fail."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    # JazzHR returns dates like "2024-01-15 12:34:56", "2024-01-15T12:34:56Z",
    # or "2024-01-15".
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_job(
    job: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Turn a JazzHR job posting into a `NormalizedDocument`."""
    job_id = str(job.get("id", ""))
    title = job.get("title") or "(untitled job)"
    description = job.get("description") or ""
    board_code = job.get("board_code", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{job_id}",
        source_id=job_id,
        title=title,
        content=description,
        content_type="html" if "<" in description else "text",
        source_url=f"{_APP_BASE}/jobs/{job_id}" if job_id else None,
        author=job.get("hiring_lead", ""),
        created_at=_parse_date(job.get("original_open_date")),
        updated_at=_parse_date(job.get("updated_at")),
        source="jazzhr",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "job",
            "status": job.get("status", ""),
            "department": job.get("department", ""),
            "city": job.get("city", ""),
            "state": job.get("state", ""),
            "country_id": job.get("country_id", ""),
            "type": job.get("type", ""),
            "board_code": board_code,
        },
    )


def normalize_applicant(
    applicant: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Turn a JazzHR applicant into a `NormalizedDocument`."""
    applicant_id = str(applicant.get("id", ""))
    first = applicant.get("first_name", "")
    last = applicant.get("last_name", "")
    full_name = (f"{first} {last}").strip() or "(unnamed applicant)"
    return NormalizedDocument(
        id=f"{tenant_id}_{applicant_id}",
        source_id=applicant_id,
        title=full_name,
        content=applicant.get("cover_letter") or applicant.get("description") or "",
        content_type="text",
        source_url=(
            f"{_APP_BASE}/applicants/{applicant_id}" if applicant_id else None
        ),
        author=applicant.get("email", ""),
        created_at=_parse_date(applicant.get("apply_date")),
        updated_at=_parse_date(applicant.get("updated_at")),
        source="jazzhr",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "applicant",
            "first_name": first,
            "last_name": last,
            "email": applicant.get("email", ""),
            "phone": applicant.get("phone", ""),
            "city": applicant.get("city", ""),
            "state": applicant.get("state", ""),
            "country_id": applicant.get("country_id", ""),
        },
    )


def normalize_note(
    note: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Turn a JazzHR note into a `NormalizedDocument`."""
    note_id = str(note.get("id", ""))
    contents = note.get("contents", "")
    applicant_id = str(note.get("applicant_id", ""))
    return NormalizedDocument(
        id=f"{tenant_id}_{note_id}",
        source_id=note_id,
        title=(
            f"Note on applicant {applicant_id}" if applicant_id else "Note"
        ),
        content=contents,
        content_type="text",
        author=note.get("user_id", ""),
        created_at=_parse_date(note.get("created_at")),
        source="jazzhr",
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "kind": "note",
            "applicant_id": applicant_id,
            "security": note.get("security", "public"),
            "user_id": note.get("user_id", ""),
        },
    )
