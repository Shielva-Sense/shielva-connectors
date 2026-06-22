from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import GreenhouseAuthError, GreenhouseError, GreenhouseRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

HARVEST_BASE = "https://harvest.greenhouse.io/v1"


def _sha256_prefix(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def normalize_job(
    job: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Greenhouse job dict into a ConnectorDocument.

    source_id = SHA-256("job:" + str(job_id))[:16]
    """
    job_id: str = str(job.get("id", ""))
    title: str = job.get("name", job_id)
    status: str = job.get("status", "")
    requisition_id: str = str(job.get("requisition_id") or "")

    departments: list[str] = [
        d.get("name", "") for d in (job.get("departments") or []) if isinstance(d, dict)
    ]
    offices: list[str] = [
        o.get("name", "") for o in (job.get("offices") or []) if isinstance(o, dict)
    ]

    content_parts: list[str] = [f"Title: {title}"]
    if status:
        content_parts.append(f"Status: {status}")
    if requisition_id:
        content_parts.append(f"Requisition ID: {requisition_id}")
    if departments:
        content_parts.append(f"Departments: {', '.join(d for d in departments if d)}")
    if offices:
        content_parts.append(f"Offices: {', '.join(o for o in offices if o)}")
    if job.get("notes"):
        content_parts.append(f"Notes: {job['notes']}")

    source_id = _sha256_prefix(f"job:{job_id}")
    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{HARVEST_BASE}/jobs/{job_id}",
        metadata={
            "resource_type": "job",
            "job_id": job_id,
            "status": status,
            "requisition_id": requisition_id,
            "departments": departments,
            "offices": offices,
            "created_at": job.get("created_at", ""),
            "opened_at": job.get("opened_at", ""),
            "closed_at": job.get("closed_at", ""),
        },
    )


def normalize_candidate(
    candidate: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Greenhouse candidate dict into a ConnectorDocument.

    source_id = SHA-256("candidate:" + str(candidate_id))[:16]
    """
    candidate_id: str = str(candidate.get("id", ""))
    first_name: str = candidate.get("first_name", "") or ""
    last_name: str = candidate.get("last_name", "") or ""
    full_name: str = f"{first_name} {last_name}".strip() or candidate_id

    email_addresses: list[str] = [
        e.get("value", "") for e in (candidate.get("email_addresses") or []) if isinstance(e, dict)
    ]
    phone_numbers: list[str] = [
        p.get("value", "") for p in (candidate.get("phone_numbers") or []) if isinstance(p, dict)
    ]
    tags: list[str] = [str(t) for t in (candidate.get("tags") or [])]

    content_parts: list[str] = [f"Name: {full_name}"]
    if email_addresses:
        content_parts.append(f"Email: {', '.join(e for e in email_addresses if e)}")
    if phone_numbers:
        content_parts.append(f"Phone: {', '.join(p for p in phone_numbers if p)}")
    if candidate.get("title"):
        content_parts.append(f"Title: {candidate['title']}")
    if candidate.get("company"):
        content_parts.append(f"Company: {candidate['company']}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")

    source_id = _sha256_prefix(f"candidate:{candidate_id}")
    return ConnectorDocument(
        source_id=source_id,
        title=full_name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{HARVEST_BASE}/candidates/{candidate_id}",
        metadata={
            "resource_type": "candidate",
            "candidate_id": candidate_id,
            "first_name": first_name,
            "last_name": last_name,
            "email_addresses": email_addresses,
            "phone_numbers": phone_numbers,
            "tags": tags,
            "created_at": candidate.get("created_at", ""),
            "updated_at": candidate.get("updated_at", ""),
            "is_private": candidate.get("is_private", False),
        },
    )


def normalize_application(
    application: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Greenhouse application dict into a ConnectorDocument.

    source_id = SHA-256("application:" + str(application_id))[:16]
    """
    application_id: str = str(application.get("id", ""))
    status: str = application.get("status", "")
    stage: str = ""
    current_stage = application.get("current_stage")
    if isinstance(current_stage, dict):
        stage = current_stage.get("name", "")

    candidate_id: str = str(application.get("candidate_id", "") or "")
    job_id: str = ""
    jobs: list[dict[str, Any]] = application.get("jobs") or []
    job_names: list[str] = []
    if jobs and isinstance(jobs[0], dict):
        job_id = str(jobs[0].get("id", ""))
        job_names = [j.get("name", "") for j in jobs if isinstance(j, dict)]

    title = f"Application {application_id}"
    if job_names:
        title = f"Application for {', '.join(n for n in job_names if n)}"

    content_parts: list[str] = [f"Application ID: {application_id}"]
    if status:
        content_parts.append(f"Status: {status}")
    if stage:
        content_parts.append(f"Current Stage: {stage}")
    if candidate_id:
        content_parts.append(f"Candidate ID: {candidate_id}")
    if job_names:
        content_parts.append(f"Jobs: {', '.join(n for n in job_names if n)}")
    if application.get("source") and isinstance(application["source"], dict):
        source_name = application["source"].get("public_name", "")
        if source_name:
            content_parts.append(f"Source: {source_name}")
    if application.get("rejection_reason") and isinstance(application["rejection_reason"], dict):
        rejection = application["rejection_reason"].get("name", "")
        if rejection:
            content_parts.append(f"Rejection Reason: {rejection}")

    source_id = _sha256_prefix(f"application:{application_id}")
    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"{HARVEST_BASE}/applications/{application_id}",
        metadata={
            "resource_type": "application",
            "application_id": application_id,
            "candidate_id": candidate_id,
            "job_id": job_id,
            "job_names": job_names,
            "status": status,
            "stage": stage,
            "applied_at": application.get("applied_at", ""),
            "rejected_at": application.get("rejected_at", ""),
            "last_activity_at": application.get("last_activity_at", ""),
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

    GreenhouseAuthError is never retried — it requires human intervention.
    GreenhouseRateLimitError honours the Retry-After value when present.
    """
    last_exc: GreenhouseError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except GreenhouseAuthError:
            raise
        except GreenhouseRateLimitError as exc:
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
        except GreenhouseError as exc:
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
