from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import WorkableAuthError, WorkableError, WorkableRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

WORKABLE_BASE = "https://{subdomain}.workable.com/spi/v3"


def _sha256_prefix(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def normalize_job(
    job: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Workable job dict into a ConnectorDocument.

    source_id = SHA-256("job:" + shortcode)[:16]
    """
    shortcode: str = str(job.get("shortcode", ""))
    title: str = job.get("title", shortcode)
    state: str = job.get("state", "")
    department: str = job.get("department", "") or ""
    location_dict = job.get("location") or {}
    location: str = ""
    if isinstance(location_dict, dict):
        city = location_dict.get("city", "") or ""
        country = location_dict.get("country", "") or ""
        location = ", ".join(part for part in [city, country] if part)
    elif isinstance(location_dict, str):
        location = location_dict
    employment_type: str = job.get("employment_type", "") or ""
    code: str = job.get("code", "") or ""
    url: str = job.get("url", "") or ""

    content_parts: list[str] = [f"Title: {title}"]
    if state:
        content_parts.append(f"State: {state}")
    if department:
        content_parts.append(f"Department: {department}")
    if location:
        content_parts.append(f"Location: {location}")
    if employment_type:
        content_parts.append(f"Employment Type: {employment_type}")
    if code:
        content_parts.append(f"Code: {code}")
    if job.get("description"):
        content_parts.append(f"Description: {job['description']}")

    source_id = _sha256_prefix(f"job:{shortcode}")
    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=url,
        metadata={
            "resource_type": "job",
            "shortcode": shortcode,
            "state": state,
            "department": department,
            "location": location,
            "employment_type": employment_type,
            "code": code,
            "created_at": job.get("created_at", ""),
            "published_on": job.get("published_on", ""),
            "expires_on": job.get("expires_on", ""),
        },
    )


def normalize_candidate(
    candidate: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Workable candidate dict into a ConnectorDocument.

    source_id = SHA-256("candidate:" + candidate["id"])[:16]
    """
    candidate_id: str = str(candidate.get("id", ""))
    name: str = candidate.get("name", "") or candidate_id
    email: str = candidate.get("email", "") or ""
    phone: str = candidate.get("phone", "") or ""
    domain: str = candidate.get("domain", "") or ""
    job_title: str = str(candidate.get("job_title", "") or "")
    social_profiles: list[dict[str, Any]] = candidate.get("social_profiles") or []
    tags: list[str] = [str(t) for t in (candidate.get("tags") or [])]

    linkedin_url = ""
    for sp in social_profiles:
        if isinstance(sp, dict) and sp.get("type") == "linkedin":
            linkedin_url = sp.get("url", "") or ""
            break

    content_parts: list[str] = [f"Name: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if domain:
        content_parts.append(f"Domain/Company: {domain}")
    if linkedin_url:
        content_parts.append(f"LinkedIn: {linkedin_url}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    if candidate.get("summary"):
        content_parts.append(f"Summary: {candidate['summary']}")

    source_id = _sha256_prefix(f"candidate:{candidate_id}")
    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=candidate.get("profile_url", ""),
        metadata={
            "resource_type": "candidate",
            "candidate_id": candidate_id,
            "name": name,
            "email": email,
            "phone": phone,
            "domain": domain,
            "job_title": job_title,
            "tags": tags,
            "created_at": candidate.get("created_at", ""),
            "updated_at": candidate.get("updated_at", ""),
        },
    )


def normalize_stage(
    stage: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Workable stage dict into a ConnectorDocument.

    source_id = SHA-256("stage:" + stage["slug"])[:16]
    """
    slug: str = str(stage.get("slug", ""))
    name: str = stage.get("name", slug)
    kind: str = stage.get("kind", "") or ""
    position: int = int(stage.get("position", 0) or 0)

    content_parts: list[str] = [f"Stage: {name}"]
    if kind:
        content_parts.append(f"Kind: {kind}")
    content_parts.append(f"Position: {position}")

    source_id = _sha256_prefix(f"stage:{slug}")
    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "resource_type": "stage",
            "slug": slug,
            "name": name,
            "kind": kind,
            "position": position,
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

    WorkableAuthError is never retried — it requires human intervention.
    WorkableRateLimitError honours the Retry-After value when present.
    """
    last_exc: WorkableError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except WorkableAuthError:
            raise
        except WorkableRateLimitError as exc:
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
        except WorkableError as exc:
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
