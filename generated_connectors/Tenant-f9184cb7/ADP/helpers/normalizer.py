"""Pure normalization helpers — no I/O, no logging."""
from typing import Any, Dict, Optional


def _legal_name(worker: Dict[str, Any]) -> Optional[str]:
    person = worker.get("person") or {}
    legal = person.get("legalName") or {}
    formatted = legal.get("formattedName")
    if formatted:
        return formatted
    given = legal.get("givenName")
    family = legal.get("familyName1") or legal.get("familyName")
    if given and family:
        return f"{given} {family}"
    return given or family


def normalize_worker(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a heavy ADP /hr/v2/workers entry to a flat dict suitable for KB ingest."""
    aoid = raw.get("associateOID") or raw.get("aoid") or ""
    status = (raw.get("workerStatus") or {}).get("statusCode", {}).get("codeValue")
    work_assignments = raw.get("workAssignments") or []
    primary = next(
        (wa for wa in work_assignments if wa.get("primaryIndicator")),
        work_assignments[0] if work_assignments else {},
    )
    job = (primary or {}).get("jobTitle")
    return {
        "aoid": aoid,
        "associate_oid": aoid,
        "legal_name": _legal_name(raw),
        "work_assignment_status": status,
        "job_title": job,
        "hire_date": (primary or {}).get("hireDate"),
        "raw": raw,
    }
