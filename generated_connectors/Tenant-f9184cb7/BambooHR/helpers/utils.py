from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import BambooHRAuthError, BambooHRError, BambooHRRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: BambooHRError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except BambooHRAuthError:
            raise
        except BambooHRRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = (
                exc.retry_after
                if exc.retry_after > 0
                else min(
                    base_delay * (RETRY_BACKOFF_FACTOR**attempt)
                    + random.uniform(0, RETRY_JITTER_S),
                    max_delay,
                )
            )
            await asyncio.sleep(delay)
        except BambooHRError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR**attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _short_hash(value: str) -> str:
    """Return a 16-character hex digest of SHA-256 for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def normalize_employee(
    employee: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    company_domain: str,
) -> ConnectorDocument:
    """Convert a raw BambooHR employee record into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of "employee:{id}" so it
    fits within Shielva's canonical 16-char source_id budget while remaining
    deterministic and collision-resistant.
    """
    employee_id: str = str(employee.get("id", ""))
    first_name: str = employee.get("firstName", "") or employee.get("first_name", "") or ""
    last_name: str = employee.get("lastName", "") or employee.get("last_name", "") or ""
    display_name: str = employee.get("displayName", "") or employee.get("display_name", "") or ""
    full_name: str = display_name or f"{first_name} {last_name}".strip() or f"Employee {employee_id}"

    job_title: str = employee.get("jobTitle", "") or employee.get("job_title", "") or ""
    department: str = employee.get("department", "") or ""
    location: str = employee.get("location", "") or ""
    work_email: str = employee.get("workEmail", "") or employee.get("work_email", "") or ""
    mobile_phone: str = employee.get("mobilePhone", "") or employee.get("mobile_phone", "") or ""
    hire_date: str = employee.get("hireDate", "") or employee.get("hire_date", "") or ""
    status: str = employee.get("status", "") or ""

    # Build human-readable content
    content_parts: list[str] = [f"Name: {full_name}"]
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if department:
        content_parts.append(f"Department: {department}")
    if location:
        content_parts.append(f"Location: {location}")
    if work_email:
        content_parts.append(f"Work Email: {work_email}")
    if mobile_phone:
        content_parts.append(f"Mobile Phone: {mobile_phone}")
    if hire_date:
        content_parts.append(f"Hire Date: {hire_date}")
    if status:
        content_parts.append(f"Status: {status}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"employee:{employee_id}")
    title = f"Employee: {full_name}"
    source_url = (
        f"https://{company_domain}.bamboohr.com/employees/employee.php?id={employee_id}"
    )

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "employee_id": employee_id,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "job_title": job_title,
            "department": department,
            "location": location,
            "work_email": work_email,
            "mobile_phone": mobile_phone,
            "hire_date": hire_date,
            "status": status,
        },
    )
