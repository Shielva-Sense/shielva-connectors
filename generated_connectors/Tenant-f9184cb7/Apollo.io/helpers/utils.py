from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import ApolloAuthError, ApolloError, ApolloRateLimitError
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
    All other ApolloError subclasses are retried up to max_attempts.
    """
    last_exc: ApolloError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except ApolloAuthError:
            raise
        except ApolloRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except ApolloError as exc:
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


def normalize_person(
    p: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert an Apollo.io mixed_people/search person record into a ConnectorDocument.

    Stable ID: SHA-256("person:" + p["id"])[:16]
    """
    record_id: str = p.get("id", "")
    first_name: str = p.get("first_name", "")
    last_name: str = p.get("last_name", "")
    name: str = p.get("name", "") or " ".join(filter(None, [first_name, last_name])) or record_id or "Unknown Person"
    email: str = p.get("email", "")
    title: str = p.get("title", "")
    company_name: str = ""
    org = p.get("organization")
    if isinstance(org, dict):
        company_name = org.get("name", "")
    if not company_name:
        company_name = p.get("organization_name", "")
    phone: str = p.get("phone_numbers", [{}])[0].get("sanitized_number", "") if p.get("phone_numbers") else ""
    city: str = p.get("city", "")
    state: str = p.get("state", "")
    country: str = p.get("country", "")
    location_parts = [part for part in (city, state, country) if part]
    location: str = ", ".join(location_parts)
    linkedin_url: str = p.get("linkedin_url", "")

    content_parts = [f"Person: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if title:
        content_parts.append(f"Title: {title}")
    if company_name:
        content_parts.append(f"Company: {company_name}")
    if location:
        content_parts.append(f"Location: {location}")
    if phone:
        content_parts.append(f"Phone: {phone}")

    stable = _stable_id("person", record_id) if record_id else _stable_id("person", name)

    return ConnectorDocument(
        source_id=stable,
        title=f"Person: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=linkedin_url,
        metadata={
            "type": "person",
            "id": record_id,
            "name": name,
            "email": email,
            "title": title,
            "company": company_name,
            "location": location,
            "phone": phone,
            "linkedin_url": linkedin_url,
        },
    )


def normalize_contact(
    c: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert an Apollo.io contacts/search contact record into a ConnectorDocument.

    Stable ID: SHA-256("contact:" + c["id"])[:16]
    """
    record_id: str = c.get("id", "")
    first_name: str = c.get("first_name", "")
    last_name: str = c.get("last_name", "")
    name: str = c.get("name", "") or " ".join(filter(None, [first_name, last_name])) or record_id or "Unknown Contact"
    email: str = c.get("email", "")
    title: str = c.get("title", "")
    company_name: str = ""
    account = c.get("account")
    if isinstance(account, dict):
        company_name = account.get("name", "")
    if not company_name:
        company_name = c.get("organization_name", "")
    phone: str = c.get("phone_numbers", [{}])[0].get("sanitized_number", "") if c.get("phone_numbers") else ""
    city: str = c.get("city", "")
    state: str = c.get("state", "")
    country: str = c.get("country", "")
    location_parts = [part for part in (city, state, country) if part]
    location: str = ", ".join(location_parts)
    linkedin_url: str = c.get("linkedin_url", "")
    label_names: list[str] = c.get("label_names", [])

    content_parts = [f"Contact: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if title:
        content_parts.append(f"Title: {title}")
    if company_name:
        content_parts.append(f"Company: {company_name}")
    if location:
        content_parts.append(f"Location: {location}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if label_names:
        content_parts.append(f"Labels: {', '.join(label_names)}")

    stable = _stable_id("contact", record_id) if record_id else _stable_id("contact", name)

    return ConnectorDocument(
        source_id=stable,
        title=f"Contact: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=linkedin_url,
        metadata={
            "type": "contact",
            "id": record_id,
            "name": name,
            "email": email,
            "title": title,
            "company": company_name,
            "location": location,
            "phone": phone,
            "linkedin_url": linkedin_url,
            "label_names": label_names,
        },
    )


def normalize_account(
    a: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert an Apollo.io accounts/search account record into a ConnectorDocument.

    Stable ID: SHA-256("account:" + a["id"])[:16]
    """
    record_id: str = a.get("id", "")
    name: str = a.get("name", record_id or "Unknown Account")
    domain: str = a.get("domain", "")
    website_url: str = a.get("website_url", "")
    industry: str = a.get("industry", "")
    employee_count: Any = a.get("num_employees", None)
    city: str = a.get("city", "")
    state: str = a.get("state", "")
    country: str = a.get("country", "")
    location_parts = [part for part in (city, state, country) if part]
    location: str = ", ".join(location_parts)
    phone: str = a.get("phone", "")
    description: str = a.get("short_description", "")
    linkedin_url: str = a.get("linkedin_url", "")
    account_stage: str = ""
    stage = a.get("account_stage")
    if isinstance(stage, dict):
        account_stage = stage.get("name", "")
    elif isinstance(stage, str):
        account_stage = stage

    content_parts = [f"Account: {name}"]
    if domain:
        content_parts.append(f"Domain: {domain}")
    if industry:
        content_parts.append(f"Industry: {industry}")
    if location:
        content_parts.append(f"Location: {location}")
    if description:
        content_parts.append(f"Description: {description}")
    if employee_count is not None:
        content_parts.append(f"Employees: {employee_count}")
    if account_stage:
        content_parts.append(f"Stage: {account_stage}")

    stable = _stable_id("account", record_id) if record_id else _stable_id("account", name)

    return ConnectorDocument(
        source_id=stable,
        title=f"Account: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=website_url or (f"https://{domain}" if domain else ""),
        metadata={
            "type": "account",
            "id": record_id,
            "name": name,
            "domain": domain,
            "industry": industry,
            "location": location,
            "employee_count": employee_count,
            "phone": phone,
            "description": description,
            "linkedin_url": linkedin_url,
            "account_stage": account_stage,
        },
    )
