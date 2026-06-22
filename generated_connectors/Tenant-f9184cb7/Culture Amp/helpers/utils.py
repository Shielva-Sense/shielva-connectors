from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import CultureAmpAuthError, CultureAmpError, CultureAmpRateLimitError
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
    last_exc: CultureAmpError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except CultureAmpAuthError:
            raise
        except CultureAmpRateLimitError as exc:
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
        except CultureAmpError as exc:
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


def normalize_survey(
    survey: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Culture Amp survey record into a ConnectorDocument.

    The source_id is SHA-256('survey:{id}')[:16] — deterministic and
    collision-resistant within the 16-char Shielva budget.
    """
    survey_id: str = str(survey.get("id", ""))
    name: str = survey.get("name", "") or survey.get("title", "") or f"Survey {survey_id}"
    status: str = survey.get("status", "") or ""
    survey_type: str = survey.get("type", "") or survey.get("survey_type", "") or ""
    created_at: str = survey.get("created_at", "") or survey.get("createdAt", "") or ""
    updated_at: str = survey.get("updated_at", "") or survey.get("updatedAt", "") or ""
    description: str = survey.get("description", "") or ""
    participants: int = int(survey.get("participants", 0) or 0)

    content_parts: list[str] = [f"Survey: {name}"]
    if survey_type:
        content_parts.append(f"Type: {survey_type}")
    if status:
        content_parts.append(f"Status: {status}")
    if description:
        content_parts.append(f"Description: {description}")
    if participants:
        content_parts.append(f"Participants: {participants}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"survey:{survey_id}")
    title = f"Survey: {name}"
    source_url = f"https://app.cultureamp.com/surveys/{survey_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "survey_id": survey_id,
            "name": name,
            "type": survey_type,
            "status": status,
            "description": description,
            "participants": participants,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_employee(
    employee: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Culture Amp employee record into a ConnectorDocument.

    The source_id is SHA-256('employee:{id}')[:16].
    """
    employee_id: str = str(employee.get("id", ""))
    first_name: str = employee.get("first_name", "") or employee.get("firstName", "") or ""
    last_name: str = employee.get("last_name", "") or employee.get("lastName", "") or ""
    email: str = employee.get("email", "") or ""
    full_name: str = (
        employee.get("name", "")
        or f"{first_name} {last_name}".strip()
        or f"Employee {employee_id}"
    )
    department: str = employee.get("department", "") or ""
    job_title: str = employee.get("job_title", "") or employee.get("jobTitle", "") or employee.get("title", "") or ""
    status: str = employee.get("status", "") or ""
    manager: str = (
        employee.get("manager", {}).get("name", "")
        if isinstance(employee.get("manager"), dict)
        else str(employee.get("manager", "") or "")
    )
    location: str = employee.get("location", "") or ""
    start_date: str = employee.get("start_date", "") or employee.get("startDate", "") or ""

    content_parts: list[str] = [f"Name: {full_name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if department:
        content_parts.append(f"Department: {department}")
    if location:
        content_parts.append(f"Location: {location}")
    if status:
        content_parts.append(f"Status: {status}")
    if manager:
        content_parts.append(f"Manager: {manager}")
    if start_date:
        content_parts.append(f"Start Date: {start_date}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"employee:{employee_id}")
    title = f"Employee: {full_name}"
    source_url = f"https://app.cultureamp.com/employees/{employee_id}"

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
            "email": email,
            "job_title": job_title,
            "department": department,
            "location": location,
            "status": status,
            "manager": manager,
            "start_date": start_date,
        },
    )


def normalize_goal(
    goal: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Culture Amp goal record into a ConnectorDocument.

    The source_id is SHA-256('goal:{id}')[:16].
    """
    goal_id: str = str(goal.get("id", ""))
    title: str = goal.get("title", "") or goal.get("name", "") or f"Goal {goal_id}"
    status: str = goal.get("status", "") or ""
    description: str = goal.get("description", "") or ""
    due_date: str = goal.get("due_date", "") or goal.get("dueDate", "") or ""
    owner: str = (
        goal.get("owner", {}).get("name", "")
        if isinstance(goal.get("owner"), dict)
        else str(goal.get("owner", "") or "")
    )
    progress: int = int(goal.get("progress", 0) or goal.get("completion_percentage", 0) or 0)
    created_at: str = goal.get("created_at", "") or goal.get("createdAt", "") or ""

    content_parts: list[str] = [f"Goal: {title}"]
    if description:
        content_parts.append(f"Description: {description}")
    if status:
        content_parts.append(f"Status: {status}")
    if owner:
        content_parts.append(f"Owner: {owner}")
    if due_date:
        content_parts.append(f"Due Date: {due_date}")
    if progress:
        content_parts.append(f"Progress: {progress}%")
    if created_at:
        content_parts.append(f"Created: {created_at}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"goal:{goal_id}")
    doc_title = f"Goal: {title}"
    source_url = f"https://app.cultureamp.com/goals/{goal_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=doc_title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "goal_id": goal_id,
            "title": title,
            "status": status,
            "description": description,
            "due_date": due_date,
            "owner": owner,
            "progress": progress,
            "created_at": created_at,
        },
    )
