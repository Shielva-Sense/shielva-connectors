from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from exceptions import RipplingAuthError, RipplingError, RipplingRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _make_id(prefix: str, entity_id: str) -> str:
    """Return a stable 16-character hex digest: SHA-256( prefix:entity_id )."""
    return hashlib.sha256(f"{prefix}:{entity_id}".encode()).hexdigest()[:16]


async def with_retry(
    coro_fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    skip_on: tuple[type[Exception], ...] = (),
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are never retried (they require human intervention).
    Any exception type listed in ``skip_on`` is re-raised immediately.
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await coro_fn(*args, **kwargs)
        except RipplingAuthError:
            raise
        except Exception as exc:
            if skip_on and isinstance(exc, skip_on):
                raise
            if isinstance(exc, RipplingRateLimitError):
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
            else:
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


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_employee(emp: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Rippling employee record into a ConnectorDocument.

    Accepts both camelCase (Rippling REST v1) and snake_case field names so
    that test fixtures can use either convention.
    """
    emp_id: str = str(emp.get("id", emp.get("_id", "")))
    first_name: str = str(
        emp.get("firstName", emp.get("first_name", ""))
    )
    last_name: str = str(
        emp.get("lastName", emp.get("last_name", ""))
    )
    full_name: str = f"{first_name} {last_name}".strip() or emp_id

    job_title: str = str(
        emp.get("jobTitle", emp.get("job_title", ""))
    )
    work_email: str = str(
        emp.get("workEmail", emp.get("work_email", ""))
    )
    department: Any = emp.get("department")
    start_date: str = str(
        emp.get("startDate", emp.get("start_date", ""))
    )
    employment_type: str = str(
        emp.get("employmentType", emp.get("employment_type", ""))
    )
    status: str = str(emp.get("status", "ACTIVE"))
    manager: Any = emp.get("manager")

    content_parts: list[str] = [f"Employee: {full_name}"]
    if job_title:
        content_parts.append(f"Title: {job_title}")
    if work_email:
        content_parts.append(f"Email: {work_email}")
    if department:
        content_parts.append(f"Department: {department}")
    if employment_type:
        content_parts.append(f"Type: {employment_type}")
    content_parts.append(f"Status: {status}")
    if start_date:
        content_parts.append(f"Start Date: {start_date}")

    source_id = _make_id("employee", emp_id)

    return ConnectorDocument(
        source_id=source_id,
        title=full_name,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        metadata={
            "employee_id": emp.get("id", emp.get("_id")),
            "email": work_email or None,
            "department": department,
            "job_title": job_title or None,
            "start_date": start_date or None,
            "employment_type": employment_type or None,
            "status": status,
            "manager": manager,
            "source": "rippling",
            "type": "employee",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def normalize_department(dept: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Rippling department record into a ConnectorDocument."""
    dept_id: str = str(dept.get("id", dept.get("_id", "")))
    name: str = str(dept.get("name", f"Department {dept_id}"))
    description: str = str(dept.get("description", ""))
    head_count: int = int(
        dept.get("headCount", dept.get("head_count", 0)) or 0
    )

    content_parts: list[str] = [f"Department: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    if head_count:
        content_parts.append(f"Head Count: {head_count}")

    source_id = _make_id("department", dept_id)

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        metadata={
            "department_id": dept.get("id", dept.get("_id")),
            "name": name,
            "head_count": head_count,
            "source": "rippling",
            "type": "department",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def normalize_team(team: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Rippling team record into a ConnectorDocument."""
    team_id: str = str(team.get("id", team.get("_id", "")))
    name: str = str(team.get("name", f"Team {team_id}"))
    description: str = str(team.get("description", ""))

    content_parts: list[str] = [f"Team: {name}"]
    if description:
        content_parts.append(f"Description: {description}")

    source_id = _make_id("team", team_id)

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        metadata={
            "team_id": team.get("id", team.get("_id")),
            "name": name,
            "source": "rippling",
            "type": "team",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def normalize_role(role: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Rippling role record into a ConnectorDocument."""
    role_id: str = str(role.get("id", role.get("_id", "")))
    name: str = str(role.get("name", f"Role {role_id}"))
    description: str = str(role.get("description", ""))

    content_parts: list[str] = [f"Role: {name}"]
    if description:
        content_parts.append(f"Description: {description}")

    source_id = _make_id("role", role_id)

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        metadata={
            "role_id": role.get("id", role.get("_id")),
            "name": name,
            "source": "rippling",
            "type": "role",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def normalize_leave(leave: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Rippling leave request into a ConnectorDocument."""
    leave_id: str = str(leave.get("id", leave.get("_id", "")))
    employee: Any = leave.get("employee", "")
    if isinstance(employee, dict):
        employee = (
            employee.get("name", "")
            or f"{employee.get('firstName', '')} {employee.get('lastName', '')}".strip()
            or str(employee.get("id", ""))
        )
    leave_type: str = str(leave.get("type", leave.get("leaveType", "")))
    start_date: str = str(leave.get("startDate", leave.get("start_date", "")))
    end_date: str = str(leave.get("endDate", leave.get("end_date", "")))
    status: str = str(leave.get("status", ""))

    content_parts: list[str] = [f"Leave Request #{leave_id}"]
    if employee:
        content_parts.append(f"Employee: {employee}")
    if leave_type:
        content_parts.append(f"Type: {leave_type}")
    if start_date:
        content_parts.append(f"Start: {start_date}")
    if end_date:
        content_parts.append(f"End: {end_date}")
    if status:
        content_parts.append(f"Status: {status}")

    source_id = _make_id("leave", leave_id)
    title = f"Leave: {employee}" if employee else f"Leave Request #{leave_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        metadata={
            "leave_id": leave.get("id", leave.get("_id")),
            "employee": employee,
            "type": leave_type,
            "start_date": start_date or None,
            "end_date": end_date or None,
            "status": status,
            "source": "rippling",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        },
    )
