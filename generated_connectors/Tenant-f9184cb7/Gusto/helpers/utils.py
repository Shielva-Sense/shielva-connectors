from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import GustoAuthError, GustoError, GustoRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _sha256_prefix(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def normalize_employee(
    employee: dict[str, Any],
    company_id: str,
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Gusto employee record into a ConnectorDocument.

    The stable source_id is derived from SHA-256("employee:" + employee_id)[:16].
    """
    employee_id: str = str(employee.get("id", employee.get("uuid", "")))
    first_name: str = employee.get("first_name", "")
    last_name: str = employee.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip() or employee_id
    email: str = employee.get("email", "")
    title: str = employee.get("job_title", "")
    department: str = employee.get("department", "")
    start_date: str = employee.get("start_date", "")
    employment_status: str = employee.get("employment_status", "")
    terminated: bool = bool(employee.get("terminated", False))

    content_parts = [f"Name: {full_name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if title:
        content_parts.append(f"Title: {title}")
    if department:
        content_parts.append(f"Department: {department}")
    if start_date:
        content_parts.append(f"Start Date: {start_date}")
    if employment_status:
        content_parts.append(f"Status: {employment_status}")
    content_parts.append(f"Terminated: {terminated}")

    source_id = _sha256_prefix(f"employee:{employee_id}", length=16)

    return ConnectorDocument(
        source_id=source_id,
        title=f"Employee: {full_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.gusto.com/companies/{company_id}/employees/{employee_id}",
        metadata={
            "employee_id": employee_id,
            "company_id": company_id,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "job_title": title,
            "department": department,
            "start_date": start_date,
            "employment_status": employment_status,
            "terminated": terminated,
        },
    )


def normalize_payroll(
    payroll: dict[str, Any],
    company_id: str,
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Gusto payroll record into a ConnectorDocument.

    The stable source_id is derived from SHA-256("payroll:" + payroll_id)[:16].
    """
    payroll_id: str = str(payroll.get("payroll_id", payroll.get("id", "")))
    period_start: str = payroll.get("pay_period", {}).get("start_date", "")
    period_end: str = payroll.get("pay_period", {}).get("end_date", "")
    check_date: str = payroll.get("check_date", "")
    processed: bool = bool(payroll.get("processed", False))
    totals: dict[str, Any] = payroll.get("totals", {})
    gross_pay: str = str(totals.get("gross_pay", "0.00"))
    net_pay: str = str(totals.get("net_pay", "0.00"))
    employee_count: int = len(payroll.get("employee_compensations", []))

    title_period = f"{period_start} – {period_end}" if period_start and period_end else payroll_id
    content_parts = [f"Payroll ID: {payroll_id}"]
    if period_start:
        content_parts.append(f"Pay Period Start: {period_start}")
    if period_end:
        content_parts.append(f"Pay Period End: {period_end}")
    if check_date:
        content_parts.append(f"Check Date: {check_date}")
    content_parts.append(f"Processed: {processed}")
    content_parts.append(f"Gross Pay: {gross_pay}")
    content_parts.append(f"Net Pay: {net_pay}")
    content_parts.append(f"Employees: {employee_count}")

    source_id = _sha256_prefix(f"payroll:{payroll_id}", length=16)

    return ConnectorDocument(
        source_id=source_id,
        title=f"Payroll: {title_period}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.gusto.com/companies/{company_id}/payrolls",
        metadata={
            "payroll_id": payroll_id,
            "company_id": company_id,
            "period_start": period_start,
            "period_end": period_end,
            "check_date": check_date,
            "processed": processed,
            "gross_pay": gross_pay,
            "net_pay": net_pay,
            "employee_count": employee_count,
        },
    )


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    GustoAuthError is never retried — it requires human intervention.
    GustoRateLimitError honours the Retry-After header when present.
    """
    last_exc: GustoError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except GustoAuthError:
            raise
        except GustoRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = (
                exc.retry_after
                if exc.retry_after > 0
                else min(
                    base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                    + random.uniform(0, RETRY_JITTER_S),
                    max_delay,
                )
            )
            await asyncio.sleep(delay)
        except GustoError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
