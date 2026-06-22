from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import LatticeAuthError, LatticeError, LatticeRateLimitError
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
    last_exc: LatticeError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except LatticeAuthError:
            raise
        except LatticeRateLimitError as exc:
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
        except LatticeError as exc:
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


def normalize_user(
    user: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Lattice user record into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of "user:{id}" so it
    fits within Shielva's canonical 16-char source_id budget while
    remaining deterministic and collision-resistant.
    """
    user_id: str = str(user.get("id", ""))
    first_name: str = user.get("firstName", "") or user.get("first_name", "") or ""
    last_name: str = user.get("lastName", "") or user.get("last_name", "") or ""
    display_name: str = user.get("displayName", "") or user.get("display_name", "") or ""
    full_name: str = display_name or f"{first_name} {last_name}".strip() or f"User {user_id}"

    email: str = user.get("email", "") or ""
    department: str = (
        user.get("department", "")
        or (user.get("department_name", ""))
        or (user.get("department", {}).get("name", "") if isinstance(user.get("department"), dict) else "")
        or ""
    )
    job_title: str = user.get("jobTitle", "") or user.get("title", "") or user.get("job_title", "") or ""
    status: str = user.get("status", "") or user.get("employmentStatus", "") or ""
    manager_id: str = str(user.get("managerId", "") or user.get("manager_id", "") or "")
    start_date: str = user.get("startDate", "") or user.get("start_date", "") or ""

    content_parts: list[str] = [f"Name: {full_name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if department:
        content_parts.append(f"Department: {department}")
    if status:
        content_parts.append(f"Status: {status}")
    if manager_id:
        content_parts.append(f"Manager ID: {manager_id}")
    if start_date:
        content_parts.append(f"Start Date: {start_date}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"user:{user_id}")
    title = f"Employee: {full_name}"
    source_url = f"https://lattice.com/people/{user_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "user_id": user_id,
            "first_name": first_name,
            "last_name": last_name,
            "full_name": full_name,
            "email": email,
            "job_title": job_title,
            "department": department,
            "status": status,
            "manager_id": manager_id,
            "start_date": start_date,
        },
    )


def normalize_goal(
    goal: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Lattice goal/OKR record into a ConnectorDocument.

    source_id is a 16-char SHA-256 prefix of "goal:{id}".
    """
    goal_id: str = str(goal.get("id", ""))
    name: str = goal.get("name", "") or goal.get("title", "") or f"Goal {goal_id}"
    description: str = goal.get("description", "") or ""
    status: str = goal.get("status", "") or goal.get("progress_status", "") or ""
    progress: str = str(goal.get("progress", "") or goal.get("completion_percentage", "") or "")
    owner_id: str = str(goal.get("ownerId", "") or goal.get("owner_id", "") or "")
    owner_name: str = goal.get("ownerName", "") or goal.get("owner_name", "") or ""
    due_date: str = goal.get("dueDate", "") or goal.get("due_date", "") or ""
    goal_type: str = goal.get("type", "") or goal.get("goal_type", "") or ""

    content_parts: list[str] = [f"Goal: {name}"]
    if description:
        content_parts.append(f"Description: {description}")
    if status:
        content_parts.append(f"Status: {status}")
    if progress:
        content_parts.append(f"Progress: {progress}%")
    if owner_name:
        content_parts.append(f"Owner: {owner_name}")
    if goal_type:
        content_parts.append(f"Type: {goal_type}")
    if due_date:
        content_parts.append(f"Due Date: {due_date}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"goal:{goal_id}")
    title = f"Goal: {name}"
    source_url = f"https://lattice.com/goals/{goal_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "goal_id": goal_id,
            "name": name,
            "description": description,
            "status": status,
            "progress": progress,
            "owner_id": owner_id,
            "owner_name": owner_name,
            "due_date": due_date,
            "goal_type": goal_type,
        },
    )


def normalize_review(
    review: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Lattice performance review record into a ConnectorDocument.

    source_id is a 16-char SHA-256 prefix of "review:{id}".
    """
    review_id: str = str(review.get("id", ""))
    title: str = review.get("title", "") or review.get("name", "") or f"Review {review_id}"
    status: str = review.get("status", "") or ""
    score: str = str(review.get("score", "") or review.get("rating", "") or "")
    reviewee_id: str = str(review.get("revieweeId", "") or review.get("reviewee_id", "") or "")
    reviewee_name: str = review.get("revieweeName", "") or review.get("reviewee_name", "") or ""
    reviewer_id: str = str(review.get("reviewerId", "") or review.get("reviewer_id", "") or "")
    reviewer_name: str = review.get("reviewerName", "") or review.get("reviewer_name", "") or ""
    period: str = review.get("period", "") or review.get("review_period", "") or ""
    due_date: str = review.get("dueDate", "") or review.get("due_date", "") or ""

    content_parts: list[str] = [f"Review: {title}"]
    if reviewee_name:
        content_parts.append(f"Reviewee: {reviewee_name}")
    if reviewer_name:
        content_parts.append(f"Reviewer: {reviewer_name}")
    if status:
        content_parts.append(f"Status: {status}")
    if score:
        content_parts.append(f"Score: {score}")
    if period:
        content_parts.append(f"Period: {period}")
    if due_date:
        content_parts.append(f"Due Date: {due_date}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"review:{review_id}")
    doc_title = f"Performance Review: {title}"
    source_url = f"https://lattice.com/reviews/{review_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=doc_title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "review_id": review_id,
            "title": title,
            "status": status,
            "score": score,
            "reviewee_id": reviewee_id,
            "reviewee_name": reviewee_name,
            "reviewer_id": reviewer_id,
            "reviewer_name": reviewer_name,
            "period": period,
            "due_date": due_date,
        },
    )
