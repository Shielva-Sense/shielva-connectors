"""Utility functions for the Workday connector.

Normalizers convert raw Workday API records into canonical ConnectorDocument
objects with stable 16-character SHA-256-derived source IDs.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import WorkdayAuthError, WorkdayError, WorkdayRateLimitError
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
    Rate-limit errors honour retry_after when present.
    """
    last_exc: WorkdayError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except WorkdayAuthError:
            raise
        except WorkdayRateLimitError as exc:
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
        except WorkdayError as exc:
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


def _short_hash(value: str) -> str:
    """Return a 16-character hex digest of SHA-256 for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _make_id(prefix: str, entity_id: str) -> str:
    """Return a stable 16-character ID for prefix:entity_id (spec alias for _short_hash)."""
    return _short_hash(f"{prefix}:{entity_id}")


def _extract_id(raw: dict[str, Any]) -> str:
    """Extract the canonical Workday resource ID from a raw record."""
    # Workday REST responses wrap IDs in a descriptor object or plain string
    wid = raw.get("id", "") or raw.get("workdayId", "") or raw.get("wid", "")
    if isinstance(wid, dict):
        return str(wid.get("id", "") or wid.get("descriptor", "") or "")
    return str(wid)


def _extract_name(raw: dict[str, Any], fallback: str = "") -> str:
    """Extract display name from a Workday record."""
    name = (
        raw.get("descriptor", "")
        or raw.get("name", "")
        or raw.get("displayName", "")
        or raw.get("workerDescriptor", "")
        or fallback
    )
    if isinstance(name, dict):
        return str(name.get("descriptor", "") or name.get("value", "") or "")
    return str(name)


# ── Worker normalizer ─────────────────────────────────────────────────────────

def normalize_worker(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    base_url: str = "",
) -> ConnectorDocument:
    """Convert a raw Workday worker record into a ConnectorDocument.

    The source_id is sha256("worker:{id}")[:16] — stable and collision-resistant.
    """
    worker_id = _extract_id(raw)
    full_name = _extract_name(raw, fallback=f"Worker {worker_id}")

    # Extract structured fields
    job_title: str = str(
        raw.get("primaryJob", {}).get("jobTitle", "")
        or raw.get("jobTitle", "")
        or raw.get("primaryJobTitle", "")
        or ""
    )
    employee_type: str = str(
        raw.get("workerType", {}).get("descriptor", "")
        or raw.get("workerType", "")
        or raw.get("employeeType", "")
        or ""
    )
    location: str = str(
        raw.get("primaryWorkAddress", {}).get("descriptor", "")
        or raw.get("location", {}).get("descriptor", "")
        or raw.get("primaryLocation", "")
        or ""
    )
    department: str = str(
        raw.get("primarySupervisoryOrg", {}).get("descriptor", "")
        or raw.get("supervisoryOrganization", {}).get("descriptor", "")
        or raw.get("department", "")
        or ""
    )
    email: str = str(
        raw.get("primaryEmail", "")
        or raw.get("workEmail", "")
        or raw.get("email", "")
        or ""
    )
    hire_date: str = str(raw.get("hireDate", "") or raw.get("startDate", "") or "")
    status: str = str(
        raw.get("active", "")
        or raw.get("workerStatus", {}).get("descriptor", "")
        or raw.get("status", "")
        or ""
    )

    content_parts: list[str] = [f"Worker: {full_name}"]
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if employee_type:
        content_parts.append(f"Worker Type: {employee_type}")
    if department:
        content_parts.append(f"Department: {department}")
    if location:
        content_parts.append(f"Location: {location}")
    if email:
        content_parts.append(f"Email: {email}")
    if hire_date:
        content_parts.append(f"Hire Date: {hire_date}")
    if status:
        content_parts.append(f"Status: {status}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"worker:{worker_id}")
    title = f"Worker: {full_name}"
    source_url = f"{base_url.rstrip('/')}/d/task/1422$3010.htmld" if base_url else ""

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "worker_id": worker_id,
            "full_name": full_name,
            "job_title": job_title,
            "employee_type": employee_type,
            "department": department,
            "location": location,
            "email": email,
            "hire_date": hire_date,
            "status": status,
        },
    )


# ── Organization normalizer ───────────────────────────────────────────────────

def normalize_organization(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    base_url: str = "",
) -> ConnectorDocument:
    """Convert a raw Workday organization record into a ConnectorDocument."""
    org_id = _extract_id(raw)
    org_name = _extract_name(raw, fallback=f"Organization {org_id}")

    org_type: str = str(
        raw.get("orgType", {}).get("descriptor", "")
        or raw.get("organizationType", "")
        or raw.get("type", "")
        or ""
    )
    manager: str = str(
        raw.get("manager", {}).get("descriptor", "")
        or raw.get("managerDescriptor", "")
        or ""
    )
    top_level: str = str(
        raw.get("topLevelOrganization", {}).get("descriptor", "")
        or raw.get("topLevel", "")
        or ""
    )
    member_count: str = str(raw.get("memberCount", "") or raw.get("headcount", "") or "")
    location: str = str(
        raw.get("location", {}).get("descriptor", "")
        or raw.get("primaryLocation", "")
        or ""
    )

    content_parts: list[str] = [f"Organization: {org_name}"]
    if org_type:
        content_parts.append(f"Type: {org_type}")
    if manager:
        content_parts.append(f"Manager: {manager}")
    if top_level:
        content_parts.append(f"Top-Level Org: {top_level}")
    if member_count:
        content_parts.append(f"Member Count: {member_count}")
    if location:
        content_parts.append(f"Location: {location}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"organization:{org_id}")
    title = f"Organization: {org_name}"
    source_url = f"{base_url.rstrip('/')}/d/task/1422$3010.htmld" if base_url else ""

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "org_id": org_id,
            "org_name": org_name,
            "org_type": org_type,
            "manager": manager,
            "top_level_org": top_level,
            "member_count": member_count,
            "location": location,
        },
    )


# ── Job Profile normalizer ────────────────────────────────────────────────────

def normalize_job_profile(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    base_url: str = "",
) -> ConnectorDocument:
    """Convert a raw Workday job profile record into a ConnectorDocument."""
    profile_id = _extract_id(raw)
    profile_name = _extract_name(raw, fallback=f"Job Profile {profile_id}")

    job_family: str = str(
        raw.get("jobFamily", {}).get("descriptor", "")
        or raw.get("jobFamilyGroup", {}).get("descriptor", "")
        or raw.get("jobFamily", "")
        or ""
    )
    management_level: str = str(
        raw.get("managementLevel", {}).get("descriptor", "")
        or raw.get("managementLevel", "")
        or ""
    )
    job_level: str = str(
        raw.get("jobLevel", {}).get("descriptor", "")
        or raw.get("jobLevel", "")
        or ""
    )
    pay_rate_type: str = str(
        raw.get("payRateType", {}).get("descriptor", "")
        or raw.get("payRateType", "")
        or ""
    )
    active: str = str(raw.get("active", "") or raw.get("isActive", "") or "")
    summary: str = str(raw.get("summary", "") or raw.get("description", "") or "")

    content_parts: list[str] = [f"Job Profile: {profile_name}"]
    if job_family:
        content_parts.append(f"Job Family: {job_family}")
    if management_level:
        content_parts.append(f"Management Level: {management_level}")
    if job_level:
        content_parts.append(f"Job Level: {job_level}")
    if pay_rate_type:
        content_parts.append(f"Pay Rate Type: {pay_rate_type}")
    if active:
        content_parts.append(f"Active: {active}")
    if summary:
        content_parts.append(f"Summary: {summary}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"job_profile:{profile_id}")
    title = f"Job Profile: {profile_name}"
    source_url = f"{base_url.rstrip('/')}/d/task/1422$3010.htmld" if base_url else ""

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "profile_id": profile_id,
            "profile_name": profile_name,
            "job_family": job_family,
            "management_level": management_level,
            "job_level": job_level,
            "pay_rate_type": pay_rate_type,
            "active": active,
            "summary": summary,
        },
    )


# ── Location normalizer ───────────────────────────────────────────────────────

def normalize_location(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
    base_url: str = "",
) -> ConnectorDocument:
    """Convert a raw Workday location record into a ConnectorDocument."""
    location_id = _extract_id(raw)
    location_name = _extract_name(raw, fallback=f"Location {location_id}")

    location_type: str = str(
        raw.get("locationType", {}).get("descriptor", "")
        or raw.get("type", "")
        or ""
    )
    address: str = str(
        raw.get("addressLine1", "")
        or raw.get("address", {}).get("formattedAddress", "")
        or raw.get("addressFormatted", "")
        or ""
    )
    city: str = str(raw.get("city", "") or raw.get("municipality", "") or "")
    country: str = str(
        raw.get("country", {}).get("descriptor", "")
        or raw.get("country", "")
        or ""
    )
    timezone: str = str(
        raw.get("timeZone", {}).get("descriptor", "")
        or raw.get("timezone", "")
        or ""
    )
    active: str = str(raw.get("active", "") or raw.get("isActive", "") or "")

    content_parts: list[str] = [f"Location: {location_name}"]
    if location_type:
        content_parts.append(f"Type: {location_type}")
    if address:
        content_parts.append(f"Address: {address}")
    if city:
        content_parts.append(f"City: {city}")
    if country:
        content_parts.append(f"Country: {country}")
    if timezone:
        content_parts.append(f"Timezone: {timezone}")
    if active:
        content_parts.append(f"Active: {active}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"location:{location_id}")
    title = f"Location: {location_name}"
    source_url = f"{base_url.rstrip('/')}/d/task/1422$3010.htmld" if base_url else ""

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "location_id": location_id,
            "location_name": location_name,
            "location_type": location_type,
            "address": address,
            "city": city,
            "country": country,
            "timezone": timezone,
            "active": active,
        },
    )
