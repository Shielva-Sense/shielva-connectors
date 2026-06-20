from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ClearbitAuthError, ClearbitError, ClearbitRateLimitError
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
    A 202 Accepted (enrichment pending) surfaces as ClearbitNotFoundError
    and IS retried, since the data may become available shortly.
    """
    last_exc: ClearbitError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ClearbitAuthError:
            raise
        except ClearbitRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ClearbitError as exc:
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


# ── Stable ID helpers ─────────────────────────────────────────────────────────


def _stable_id(prefix: str, key: str) -> str:
    """Return SHA-256(prefix + ':' + key)[:16].

    Produces a stable, collision-resistant document ID for deduplication.
    """
    raw = f"{prefix}:{key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_company(
    c: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Clearbit Company object into a ConnectorDocument.

    Stable ID: SHA-256("company:" + domain)[:16]
    """
    domain: str = c.get("domain", "")
    name: str = c.get("name", domain or "Unknown Company")
    industry: str = c.get("category", {}).get("industry", "") if isinstance(c.get("category"), dict) else ""
    city: str = c.get("geo", {}).get("city", "") if isinstance(c.get("geo"), dict) else ""
    country: str = c.get("geo", {}).get("country", "") if isinstance(c.get("geo"), dict) else ""
    location_parts = [p for p in (city, country) if p]
    location: str = ", ".join(location_parts)

    description: str = c.get("description", "")
    employees: Any = c.get("metrics", {}).get("employees", None) if isinstance(c.get("metrics"), dict) else None
    founded_year: Any = c.get("foundedYear", None)
    linkedin_handle: str = c.get("linkedin", {}).get("handle", "") if isinstance(c.get("linkedin"), dict) else ""

    content_parts = [f"Company: {name}"]
    if domain:
        content_parts.append(f"Domain: {domain}")
    if industry:
        content_parts.append(f"Industry: {industry}")
    if location:
        content_parts.append(f"Location: {location}")
    if description:
        content_parts.append(f"Description: {description}")
    if employees is not None:
        content_parts.append(f"Employees: {employees}")
    if founded_year:
        content_parts.append(f"Founded: {founded_year}")

    stable = _stable_id("company", domain) if domain else _stable_id("company", name)

    return ConnectorDocument(
        source_id=stable,
        title=f"Company: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://clearbit.com/companies/{domain}" if domain else "",
        metadata={
            "type": "company",
            "name": name,
            "domain": domain,
            "industry": industry,
            "location": location,
            "description": description,
            "employees": employees,
            "founded_year": founded_year,
            "linkedin_handle": linkedin_handle,
        },
    )


def normalize_person(
    p: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Clearbit Person object into a ConnectorDocument.

    Stable ID: SHA-256("person:" + email)[:16]
    """
    email: str = p.get("email", "")
    name: str = (
        p.get("name", {}).get("fullName", "")
        if isinstance(p.get("name"), dict)
        else p.get("name", "")
    )
    if not name:
        given = p.get("name", {}).get("givenName", "") if isinstance(p.get("name"), dict) else ""
        family = p.get("name", {}).get("familyName", "") if isinstance(p.get("name"), dict) else ""
        name = " ".join(filter(None, [given, family])) or email or "Unknown Person"

    title: str = p.get("employment", {}).get("title", "") if isinstance(p.get("employment"), dict) else ""
    company_name: str = p.get("employment", {}).get("name", "") if isinstance(p.get("employment"), dict) else ""
    linkedin_handle: str = p.get("linkedin", {}).get("handle", "") if isinstance(p.get("linkedin"), dict) else ""
    location: str = p.get("location", "")
    bio: str = p.get("bio", "")
    site: str = p.get("site", "")

    content_parts = [f"Person: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if title:
        content_parts.append(f"Title: {title}")
    if company_name:
        content_parts.append(f"Company: {company_name}")
    if location:
        content_parts.append(f"Location: {location}")
    if bio:
        content_parts.append(f"Bio: {bio}")

    stable = _stable_id("person", email) if email else _stable_id("person", name)

    return ConnectorDocument(
        source_id=stable,
        title=f"Person: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=site or "",
        metadata={
            "type": "person",
            "name": name,
            "email": email,
            "title": title,
            "company": company_name,
            "location": location,
            "linkedin_handle": linkedin_handle,
        },
    )


def normalize_combined(
    data: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Clearbit Combined (person + company) response into a ConnectorDocument.

    The combined response has keys 'person' and 'company'.
    Stable ID: SHA-256("combined:" + email)[:16] — anchored to the person's email.
    """
    person_data: dict[str, Any] = data.get("person") or {}
    company_data: dict[str, Any] = data.get("company") or {}

    email: str = person_data.get("email", "")
    person_name: str = (
        person_data.get("name", {}).get("fullName", "")
        if isinstance(person_data.get("name"), dict)
        else person_data.get("name", "")
    )
    if not person_name:
        given = person_data.get("name", {}).get("givenName", "") if isinstance(person_data.get("name"), dict) else ""
        family = person_data.get("name", {}).get("familyName", "") if isinstance(person_data.get("name"), dict) else ""
        person_name = " ".join(filter(None, [given, family])) or email or "Unknown Person"

    title: str = (
        person_data.get("employment", {}).get("title", "")
        if isinstance(person_data.get("employment"), dict)
        else ""
    )
    company_name: str = company_data.get("name", "") or (
        person_data.get("employment", {}).get("name", "")
        if isinstance(person_data.get("employment"), dict)
        else ""
    )
    company_domain: str = company_data.get("domain", "")
    industry: str = (
        company_data.get("category", {}).get("industry", "")
        if isinstance(company_data.get("category"), dict)
        else ""
    )

    content_parts = [f"Person: {person_name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if title:
        content_parts.append(f"Title: {title}")
    if company_name:
        content_parts.append(f"Company: {company_name}")
    if company_domain:
        content_parts.append(f"Company domain: {company_domain}")
    if industry:
        content_parts.append(f"Industry: {industry}")

    stable = _stable_id("combined", email) if email else _stable_id("combined", person_name)

    return ConnectorDocument(
        source_id=stable,
        title=f"Combined: {person_name} @ {company_name}" if company_name else f"Combined: {person_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://clearbit.com/companies/{company_domain}" if company_domain else "",
        metadata={
            "type": "combined",
            "person_name": person_name,
            "email": email,
            "title": title,
            "company_name": company_name,
            "company_domain": company_domain,
            "industry": industry,
        },
    )
