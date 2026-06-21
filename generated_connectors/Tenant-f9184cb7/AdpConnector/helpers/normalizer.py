"""Normalize ADP API resources into NormalizedDocument.

Pure functions — no I/O, no logging.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


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


def _primary_assignment(worker: Dict[str, Any]) -> Dict[str, Any]:
    work_assignments = worker.get("workAssignments") or []
    if not work_assignments:
        return {}
    primary = next(
        (wa for wa in work_assignments if wa.get("primaryIndicator")),
        work_assignments[0],
    )
    return primary or {}


def normalize_worker(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn an ADP /hr/v2/workers entry into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = raw.get("associateOID") or raw.get("aoid") or ""
    primary = _primary_assignment(raw)
    job_title = primary.get("jobTitle") or ""
    status = ((raw.get("workerStatus") or {}).get("statusCode") or {}).get("codeValue") or ""
    name = _legal_name(raw) or source_id

    content_parts = [p for p in (name, job_title, status) if p]
    content = " · ".join(content_parts)

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=content,
        content_type="text",
        source_url=None,
        url=None,
        author=name,
        created_at=_parse_dt(primary.get("hireDate")),
        updated_at=_parse_dt(raw.get("lastUpdated") or primary.get("hireDate")),
        metadata={
            "kind": "adp.worker",
            "associate_oid": source_id,
            "status": status,
            "job_title": job_title,
            "hire_date": primary.get("hireDate"),
        },
    )


def normalize_pay_statement(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn an ADP pay-statement entry into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = raw.get("payStatementID") or raw.get("payStatementId") or ""
    pay_date = raw.get("payDate") or ""
    net = (raw.get("netPayAmount") or {}).get("amountValue")
    currency = (raw.get("netPayAmount") or {}).get("currencyCode") or ""
    title = f"Pay statement {pay_date}" if pay_date else f"Pay statement {source_id}"
    content = f"net {net} {currency}".strip()

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        created_at=_parse_dt(pay_date),
        updated_at=_parse_dt(pay_date),
        metadata={
            "kind": "adp.pay_statement",
            "pay_date": pay_date,
            "net_pay": net,
            "currency": currency,
            "statement_status": raw.get("payStatementStatusCode", {}).get("codeValue")
            if isinstance(raw.get("payStatementStatusCode"), dict)
            else None,
        },
    )


def normalize_time_off_request(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn an ADP time-off request into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    source_id = raw.get("timeOffRequestID") or raw.get("itemID") or ""
    start = raw.get("startDate") or ""
    end = raw.get("endDate") or ""
    policy_code = (
        ((raw.get("timeOffPolicyCode") or {}).get("codeValue"))
        if isinstance(raw.get("timeOffPolicyCode"), dict)
        else ""
    )
    status = (
        ((raw.get("requestStatusCode") or {}).get("codeValue"))
        if isinstance(raw.get("requestStatusCode"), dict)
        else ""
    )
    hours = raw.get("totalTimeOffHours")
    title = f"Time off {start}–{end}" if start or end else f"Time off {source_id}"
    content = f"{policy_code} {hours} ({status})".strip()

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=content,
        content_type="text",
        created_at=_parse_dt(start),
        updated_at=_parse_dt(start),
        metadata={
            "kind": "adp.time_off_request",
            "policy_code": policy_code,
            "status": status,
            "hours": hours,
            "start_date": start,
            "end_date": end,
        },
    )
