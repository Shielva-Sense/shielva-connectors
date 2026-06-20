from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import (
    FreshworksCRMAuthError,
    FreshworksCRMError,
    FreshworksCRMRateLimitError,
)
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(prefix: str, value: str) -> str:
    """Return a 16-character hex digest (sha256 prefix) for a prefixed key."""
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_contact(
    contact: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    domain: str,
) -> ConnectorDocument:
    """Convert a raw Freshworks CRM contact into a ConnectorDocument."""
    contact_id: int = contact.get("id", 0)
    first_name: str = contact.get("first_name", "") or ""
    last_name: str = contact.get("last_name", "") or ""
    name: str = (
        contact.get("display_name", "")
        or f"{first_name} {last_name}".strip()
        or f"Contact #{contact_id}"
    )
    email: str = contact.get("email", "") or ""
    phone: str = contact.get("work_number", "") or contact.get("mobile_number", "") or ""
    job_title: str = contact.get("job_title", "") or ""
    company_name: str = contact.get("company", {}).get("name", "") if isinstance(contact.get("company"), dict) else ""
    lead_source: str = contact.get("lead_source_id", "") or ""
    linkedin: str = contact.get("linkedin", "") or ""
    created_at: str = contact.get("created_at", "") or ""
    updated_at: str = contact.get("updated_at", "") or ""
    owner_id: Any = contact.get("owner_id", None)

    content_parts: list[str] = [f"Name: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if company_name:
        content_parts.append(f"Company: {company_name}")
    if lead_source:
        content_parts.append(f"Lead Source: {lead_source}")
    if linkedin:
        content_parts.append(f"LinkedIn: {linkedin}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")
    if updated_at:
        content_parts.append(f"Updated At: {updated_at}")

    source_id = _short_id("contact", str(contact_id))

    return ConnectorDocument(
        source_id=source_id,
        title=f"Contact: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://{domain}.myfreshworks.com/crm/sales/contacts/{contact_id}",
        metadata={
            "contact_id": contact_id,
            "email": email,
            "phone": phone,
            "job_title": job_title,
            "company_name": company_name,
            "owner_id": owner_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_deal(
    deal: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    domain: str,
) -> ConnectorDocument:
    """Convert a raw Freshworks CRM deal into a ConnectorDocument."""
    deal_id: int = deal.get("id", 0)
    name: str = deal.get("name", "") or f"Deal #{deal_id}"
    amount: Any = deal.get("amount", None)
    stage_id: Any = deal.get("deal_stage_id", None)
    expected_close: str = deal.get("expected_close", "") or ""
    owner_id: Any = deal.get("owner_id", None)
    lead_source: Any = deal.get("lead_source_id", None)
    probability: Any = deal.get("probability", None)
    created_at: str = deal.get("created_at", "") or ""
    updated_at: str = deal.get("updated_at", "") or ""
    # Associated contact and account
    contact_id: Any = deal.get("fc_widget_collaboration_id", None) or deal.get("contact_id", None)
    sales_account_id: Any = deal.get("sales_account_id", None)

    content_parts: list[str] = [f"Deal: {name}"]
    if amount is not None:
        content_parts.append(f"Amount: {amount}")
    if stage_id is not None:
        content_parts.append(f"Stage ID: {stage_id}")
    if probability is not None:
        content_parts.append(f"Probability: {probability}%")
    if expected_close:
        content_parts.append(f"Expected Close: {expected_close}")
    if lead_source:
        content_parts.append(f"Lead Source ID: {lead_source}")
    if contact_id:
        content_parts.append(f"Contact ID: {contact_id}")
    if sales_account_id:
        content_parts.append(f"Account ID: {sales_account_id}")
    if owner_id:
        content_parts.append(f"Owner ID: {owner_id}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")
    if updated_at:
        content_parts.append(f"Updated At: {updated_at}")

    source_id = _short_id("deal", str(deal_id))

    return ConnectorDocument(
        source_id=source_id,
        title=f"Deal: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://{domain}.myfreshworks.com/crm/sales/deals/{deal_id}",
        metadata={
            "deal_id": deal_id,
            "amount": amount,
            "stage_id": stage_id,
            "probability": probability,
            "expected_close": expected_close,
            "owner_id": owner_id,
            "sales_account_id": sales_account_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_account(
    account: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    domain: str,
) -> ConnectorDocument:
    """Convert a raw Freshworks CRM sales account into a ConnectorDocument."""
    account_id: int = account.get("id", 0)
    name: str = account.get("name", "") or f"Account #{account_id}"
    website: str = account.get("website", "") or ""
    phone: str = account.get("phone", "") or ""
    industry_type: Any = account.get("industry_type_id", None)
    business_type: Any = account.get("business_type_id", None)
    number_of_employees: Any = account.get("number_of_employees", None)
    owner_id: Any = account.get("owner_id", None)
    annual_revenue: Any = account.get("annual_revenue", None)
    created_at: str = account.get("created_at", "") or ""
    updated_at: str = account.get("updated_at", "") or ""

    content_parts: list[str] = [f"Account: {name}"]
    if website:
        content_parts.append(f"Website: {website}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if industry_type is not None:
        content_parts.append(f"Industry Type ID: {industry_type}")
    if business_type is not None:
        content_parts.append(f"Business Type ID: {business_type}")
    if number_of_employees is not None:
        content_parts.append(f"Employees: {number_of_employees}")
    if annual_revenue is not None:
        content_parts.append(f"Annual Revenue: {annual_revenue}")
    if owner_id:
        content_parts.append(f"Owner ID: {owner_id}")
    if created_at:
        content_parts.append(f"Created At: {created_at}")
    if updated_at:
        content_parts.append(f"Updated At: {updated_at}")

    source_id = _short_id("account", str(account_id))

    return ConnectorDocument(
        source_id=source_id,
        title=f"Account: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://{domain}.myfreshworks.com/crm/sales/sales-accounts/{account_id}",
        metadata={
            "account_id": account_id,
            "website": website,
            "phone": phone,
            "industry_type_id": industry_type,
            "number_of_employees": number_of_employees,
            "annual_revenue": annual_revenue,
            "owner_id": owner_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


# ── Retry helper ──────────────────────────────────────────────────────────────


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
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: FreshworksCRMError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except FreshworksCRMAuthError:
            raise  # no retry on auth failures
        except FreshworksCRMRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except FreshworksCRMError as exc:
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
