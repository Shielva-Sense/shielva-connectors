from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import (
    SmartRecruitersAuthError,
    SmartRecruitersError,
    SmartRecruitersRateLimitError,
)
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

SR_BASE = "https://api.smartrecruiters.com"

T = TypeVar("T")


def _sha256_prefix(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def normalize_job(
    job: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a SmartRecruiters job posting dict into a ConnectorDocument.

    source_id = SHA-256("job:" + str(job_id))[:16]
    """
    job_id: str = str(job.get("id", ""))
    title: str = job.get("name", job_id) or job_id
    status: str = job.get("status", "") or ""
    ref_number: str = str(job.get("refNumber") or "")

    # Location
    location: dict[str, Any] = job.get("location") or {}
    city: str = location.get("city", "") or ""
    country: str = location.get("country", "") or ""
    location_str = ", ".join(part for part in (city, country) if part)

    # Department
    department: dict[str, Any] = job.get("department") or {}
    department_name: str = department.get("label", "") or ""

    # Company
    company_name: str = ""
    company: dict[str, Any] = job.get("company") or {}
    if company:
        company_name = company.get("name", "") or ""

    # Experience / type
    experience: dict[str, Any] = job.get("experienceLevel") or {}
    experience_label: str = experience.get("label", "") or ""

    employment_type: dict[str, Any] = job.get("typeOfEmployment") or {}
    employment_label: str = employment_type.get("label", "") or ""

    content_parts: list[str] = [f"Title: {title}"]
    if status:
        content_parts.append(f"Status: {status}")
    if ref_number:
        content_parts.append(f"Ref Number: {ref_number}")
    if department_name:
        content_parts.append(f"Department: {department_name}")
    if location_str:
        content_parts.append(f"Location: {location_str}")
    if experience_label:
        content_parts.append(f"Experience Level: {experience_label}")
    if employment_label:
        content_parts.append(f"Employment Type: {employment_label}")
    if company_name:
        content_parts.append(f"Company: {company_name}")

    source_id = _sha256_prefix(f"job:{job_id}")
    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{SR_BASE}/v1/jobs/{job_id}",
        metadata={
            "resource_type": "job_posting",
            "job_id": job_id,
            "status": status,
            "ref_number": ref_number,
            "department": department_name,
            "location": location_str,
            "city": city,
            "country": country,
            "experience_level": experience_label,
            "employment_type": employment_label,
            "company_name": company_name,
            "created_on": job.get("createdOn", ""),
            "updated_on": job.get("updatedOn", ""),
        },
    )


def normalize_candidate(
    candidate: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a SmartRecruiters candidate dict into a ConnectorDocument.

    source_id = SHA-256("candidate:" + str(candidate_id))[:16]
    """
    candidate_id: str = str(candidate.get("id", ""))
    first_name: str = candidate.get("firstName", "") or ""
    last_name: str = candidate.get("lastName", "") or ""
    full_name: str = f"{first_name} {last_name}".strip() or candidate_id

    email: str = candidate.get("email", "") or ""
    phone: str = candidate.get("phoneNumber", "") or ""

    # Location
    location: dict[str, Any] = candidate.get("location") or {}
    city: str = location.get("city", "") or ""
    country: str = location.get("country", "") or ""
    location_str = ", ".join(part for part in (city, country) if part)

    # Tags
    tags: list[str] = [str(t) for t in (candidate.get("tags") or [])]

    content_parts: list[str] = [f"Name: {full_name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if location_str:
        content_parts.append(f"Location: {location_str}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")

    source_id = _sha256_prefix(f"candidate:{candidate_id}")
    return ConnectorDocument(
        source_id=source_id,
        title=full_name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{SR_BASE}/v1/candidates/{candidate_id}",
        metadata={
            "resource_type": "candidate",
            "candidate_id": candidate_id,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone": phone,
            "location": location_str,
            "city": city,
            "country": country,
            "tags": tags,
            "created_on": candidate.get("createdOn", ""),
            "updated_on": candidate.get("updatedOn", ""),
        },
    )


def normalize_user(
    user: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a SmartRecruiters user dict into a ConnectorDocument.

    source_id = SHA-256("user:" + str(user_id))[:16]
    """
    user_id: str = str(user.get("id", ""))
    first_name: str = user.get("firstName", "") or ""
    last_name: str = user.get("lastName", "") or ""
    full_name: str = f"{first_name} {last_name}".strip() or user_id

    email: str = user.get("email", "") or ""
    role: str = user.get("role", "") or ""
    status: str = user.get("status", "") or ""

    content_parts: list[str] = [f"Name: {full_name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if role:
        content_parts.append(f"Role: {role}")
    if status:
        content_parts.append(f"Status: {status}")

    source_id = _sha256_prefix(f"user:{user_id}")
    return ConnectorDocument(
        source_id=source_id,
        title=full_name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{SR_BASE}/v1/users/{user_id}",
        metadata={
            "resource_type": "user",
            "user_id": user_id,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "role": role,
            "status": status,
            "created_on": user.get("createdOn", ""),
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

    SmartRecruitersAuthError is never retried — it requires human intervention.
    SmartRecruitersRateLimitError honours the Retry-After value when present.
    """
    last_exc: SmartRecruitersError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except SmartRecruitersAuthError:
            raise
        except SmartRecruitersRateLimitError as exc:
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
        except SmartRecruitersError as exc:
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
