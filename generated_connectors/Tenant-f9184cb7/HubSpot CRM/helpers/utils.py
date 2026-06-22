from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import HubSpotAuthError, HubSpotError, HubSpotRateLimitError
from models import ConnectorDocument

T = TypeVar("T")

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5


# ── Document ID helper ───────────────────────────────────────────────────────


def _doc_id(prefix: str, record_id: str) -> str:
    return hashlib.sha256(f"{prefix}:{record_id}".encode()).hexdigest()[:16]


# ── Normalizers ──────────────────────────────────────────────────────────────


def normalize_contact(
    contact: dict[str, Any], properties: list[str] | None = None
) -> dict[str, Any]:
    """Produce a ConnectorDocument-compatible dict from a raw HubSpot contact."""
    contact_id = contact.get("id", "")
    props = contact.get("properties") or {}

    first = props.get("firstname", "") or ""
    last = props.get("lastname", "") or ""
    name = f"{first} {last}".strip() or "Unknown"
    email = props.get("email", "") or ""
    phone = props.get("phone", "") or ""
    company = props.get("company", "") or ""
    created = props.get("createdate", "") or ""
    updated = props.get("lastmodifieddate", "") or ""

    title = f"HubSpot contact: {name}" + (f" <{email}>" if email else "")
    content_lines = [
        f"Contact ID: {contact_id}",
        f"Name: {name}",
    ]
    if email:
        content_lines.append(f"Email: {email}")
    if phone:
        content_lines.append(f"Phone: {phone}")
    if company:
        content_lines.append(f"Company: {company}")
    if created:
        content_lines.append(f"Created: {created}")
    if updated:
        content_lines.append(f"Last modified: {updated}")

    return ConnectorDocument(
        source_id=contact_id,
        title=title,
        content="\n".join(content_lines),
        connector_id="",
        tenant_id="",
        source_url=f"https://app.hubspot.com/contacts/0/contact/{contact_id}",
        metadata={
            "object_type": "contact",
            "email": email,
            "name": name,
            "phone": phone,
            "company": company,
            "createdate": created,
            "lastmodifieddate": updated,
        },
    )


def normalize_company(company: dict[str, Any]) -> dict[str, Any]:
    """Produce a ConnectorDocument-compatible dict from a raw HubSpot company."""
    company_id = company.get("id", "")
    props = company.get("properties") or {}

    name = props.get("name", "") or f"Company {company_id}"
    domain = props.get("domain", "") or ""
    industry = props.get("industry", "") or ""
    city = props.get("city", "") or ""
    country = props.get("country", "") or ""
    phone = props.get("phone", "") or ""
    employees = props.get("numberofemployees", "") or ""
    created = props.get("createdate", "") or ""

    title = f"HubSpot company: {name}" + (f" ({domain})" if domain else "")
    content_lines = [f"Company ID: {company_id}", f"Name: {name}"]
    if domain:
        content_lines.append(f"Domain: {domain}")
    if industry:
        content_lines.append(f"Industry: {industry}")
    if city or country:
        content_lines.append(f"Location: {', '.join(x for x in [city, country] if x)}")
    if phone:
        content_lines.append(f"Phone: {phone}")
    if employees:
        content_lines.append(f"Employees: {employees}")
    if created:
        content_lines.append(f"Created: {created}")

    return ConnectorDocument(
        source_id=company_id,
        title=title,
        content="\n".join(content_lines),
        connector_id="",
        tenant_id="",
        source_url=f"https://app.hubspot.com/contacts/0/company/{company_id}",
        metadata={
            "object_type": "company",
            "name": name,
            "domain": domain,
            "industry": industry,
            "city": city,
            "country": country,
            "numberofemployees": employees,
            "createdate": created,
        },
    )


def normalize_deal(deal: dict[str, Any]) -> dict[str, Any]:
    """Produce a ConnectorDocument-compatible dict from a raw HubSpot deal."""
    deal_id = deal.get("id", "")
    props = deal.get("properties") or {}

    deal_name = props.get("dealname", "") or f"Deal {deal_id}"
    amount = props.get("amount", "") or ""
    stage = props.get("dealstage", "") or ""
    pipeline = props.get("pipeline", "") or ""
    close_date = props.get("closedate", "") or ""
    created = props.get("createdate", "") or ""
    owner_id = props.get("hubspot_owner_id", "") or ""

    title = f"HubSpot deal: {deal_name}"
    if stage:
        title += f" — {stage}"
    content_lines = [f"Deal ID: {deal_id}", f"Name: {deal_name}"]
    if amount:
        content_lines.append(f"Amount: {amount}")
    if stage:
        content_lines.append(f"Stage: {stage}")
    if pipeline:
        content_lines.append(f"Pipeline: {pipeline}")
    if close_date:
        content_lines.append(f"Close date: {close_date}")
    if created:
        content_lines.append(f"Created: {created}")
    if owner_id:
        content_lines.append(f"Owner ID: {owner_id}")

    return ConnectorDocument(
        source_id=deal_id,
        title=title,
        content="\n".join(content_lines),
        connector_id="",
        tenant_id="",
        source_url=f"https://app.hubspot.com/contacts/0/deal/{deal_id}",
        metadata={
            "object_type": "deal",
            "dealname": deal_name,
            "amount": amount,
            "dealstage": stage,
            "pipeline": pipeline,
            "closedate": close_date,
            "createdate": created,
            "hubspot_owner_id": owner_id,
        },
    )


def normalize_ticket(ticket: dict[str, Any]) -> dict[str, Any]:
    """Produce a ConnectorDocument-compatible dict from a raw HubSpot ticket."""
    ticket_id = ticket.get("id", "")
    props = ticket.get("properties") or {}

    subject = props.get("subject", "") or f"Ticket {ticket_id}"
    content_body = props.get("content", "") or ""
    priority = props.get("hs_ticket_priority", "") or ""
    pipeline_stage = props.get("hs_pipeline_stage", "") or ""
    created = props.get("createdate", "") or ""
    updated = props.get("hs_lastmodifieddate", "") or ""

    title = f"HubSpot ticket: {subject}"
    content_lines = [f"Ticket ID: {ticket_id}", f"Subject: {subject}"]
    if content_body:
        content_lines.append(f"Content: {content_body}")
    if priority:
        content_lines.append(f"Priority: {priority}")
    if pipeline_stage:
        content_lines.append(f"Stage: {pipeline_stage}")
    if created:
        content_lines.append(f"Created: {created}")
    if updated:
        content_lines.append(f"Last modified: {updated}")

    return ConnectorDocument(
        source_id=ticket_id,
        title=title,
        content="\n".join(content_lines),
        connector_id="",
        tenant_id="",
        source_url=f"https://app.hubspot.com/contacts/0/ticket/{ticket_id}",
        metadata={
            "object_type": "ticket",
            "subject": subject,
            "priority": priority,
            "pipeline_stage": pipeline_stage,
            "createdate": created,
            "lastmodifieddate": updated,
        },
    )


# ── Retry ────────────────────────────────────────────────────────────────────


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    backoff_base: float = RETRY_BASE_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    - HubSpotAuthError: never retried (requires human intervention).
    - HubSpotRateLimitError: honours Retry-After header when > 0.
    - All other HubSpotError subclasses: exponential backoff.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except HubSpotAuthError:
            raise  # auth errors are never retried
        except HubSpotRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                backoff_base * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                RETRY_MAX_DELAY_S,
            )
            await asyncio.sleep(delay)
        except HubSpotError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                backoff_base * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                RETRY_MAX_DELAY_S,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
