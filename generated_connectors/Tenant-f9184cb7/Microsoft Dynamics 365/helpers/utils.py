from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from exceptions import Dynamics365AuthError, Dynamics365Error, Dynamics365RateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


# ── Retry ─────────────────────────────────────────────────────────────────────

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
    last_exc: Dynamics365Error | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except Dynamics365AuthError:
            raise  # never retry auth failures
        except Dynamics365RateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except Dynamics365Error as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Stable ID helper ──────────────────────────────────────────────────────────

def _stable_id(prefix: str, raw_id: str) -> str:
    """SHA-256 of '<prefix>:<raw_id>', truncated to 16 hex chars."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ── Normalizers ───────────────────────────────────────────────────────────────

def normalize_contact(raw: dict[str, Any], instance_url: str = "") -> ConnectorDocument:
    """Normalize a raw Dataverse contact record into a ConnectorDocument."""
    contact_id = raw.get("contactid", "")
    first = raw.get("firstname") or ""
    last = raw.get("lastname") or ""
    name = f"{first} {last}".strip() or "Unknown Contact"
    email = raw.get("emailaddress1") or ""
    phone = raw.get("telephone1") or ""
    title = raw.get("jobtitle") or ""
    account_id = raw.get("_parentcustomerid_value") or ""

    content_parts = [f"Name: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if title:
        content_parts.append(f"Title: {title}")
    if account_id:
        content_parts.append(f"Account ID: {account_id}")

    source_url = (
        f"{instance_url.rstrip('/')}/main.aspx?etn=contact&id={contact_id}"
        if instance_url and contact_id
        else ""
    )

    return ConnectorDocument(
        id=_stable_id("contact", contact_id),
        source="dynamics365",
        type="contact",
        title=name,
        content="\n".join(content_parts),
        metadata={
            "contactid": contact_id,
            "email": email,
            "phone": phone,
            "title": title,
            "account_id": account_id,
            "createdon": raw.get("createdon", ""),
            "modifiedon": raw.get("modifiedon", ""),
        },
        synced_at=_now_iso(),
        source_url=source_url,
    )


def normalize_account(raw: dict[str, Any], instance_url: str = "") -> ConnectorDocument:
    """Normalize a raw Dataverse account record into a ConnectorDocument."""
    account_id = raw.get("accountid", "")
    name = raw.get("name") or "Unknown Account"
    email = raw.get("emailaddress1") or ""
    phone = raw.get("telephone1") or ""
    website = raw.get("websiteurl") or ""
    industry = raw.get("industry") or ""
    revenue = raw.get("revenue") or ""

    content_parts = [f"Account: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if website:
        content_parts.append(f"Website: {website}")
    if industry:
        content_parts.append(f"Industry: {industry}")
    if revenue:
        content_parts.append(f"Revenue: {revenue}")

    source_url = (
        f"{instance_url.rstrip('/')}/main.aspx?etn=account&id={account_id}"
        if instance_url and account_id
        else ""
    )

    return ConnectorDocument(
        id=_stable_id("account", account_id),
        source="dynamics365",
        type="account",
        title=name,
        content="\n".join(content_parts),
        metadata={
            "accountid": account_id,
            "email": email,
            "phone": phone,
            "website": website,
            "industry": industry,
            "revenue": str(revenue) if revenue else "",
            "createdon": raw.get("createdon", ""),
            "modifiedon": raw.get("modifiedon", ""),
        },
        synced_at=_now_iso(),
        source_url=source_url,
    )


def normalize_lead(raw: dict[str, Any], instance_url: str = "") -> ConnectorDocument:
    """Normalize a raw Dataverse lead record into a ConnectorDocument."""
    lead_id = raw.get("leadid", "")
    first = raw.get("firstname") or ""
    last = raw.get("lastname") or ""
    name = f"{first} {last}".strip() or "Unknown Lead"
    company = raw.get("companyname") or ""
    email = raw.get("emailaddress1") or ""
    phone = raw.get("telephone1") or ""
    status = raw.get("statuscode") or ""
    source = raw.get("leadsourcecode") or ""

    content_parts = [f"Lead: {name}"]
    if company:
        content_parts.append(f"Company: {company}")
    if email:
        content_parts.append(f"Email: {email}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if status:
        content_parts.append(f"Status: {status}")
    if source:
        content_parts.append(f"Source: {source}")

    source_url = (
        f"{instance_url.rstrip('/')}/main.aspx?etn=lead&id={lead_id}"
        if instance_url and lead_id
        else ""
    )

    return ConnectorDocument(
        id=_stable_id("lead", lead_id),
        source="dynamics365",
        type="lead",
        title=name,
        content="\n".join(content_parts),
        metadata={
            "leadid": lead_id,
            "company": company,
            "email": email,
            "phone": phone,
            "status": str(status) if status else "",
            "lead_source": str(source) if source else "",
            "createdon": raw.get("createdon", ""),
            "modifiedon": raw.get("modifiedon", ""),
        },
        synced_at=_now_iso(),
        source_url=source_url,
    )


def normalize_opportunity(raw: dict[str, Any], instance_url: str = "") -> ConnectorDocument:
    """Normalize a raw Dataverse opportunity record into a ConnectorDocument."""
    opp_id = raw.get("opportunityid", "")
    name = raw.get("name") or "Unknown Opportunity"
    value = raw.get("estimatedvalue") or ""
    close_date = raw.get("actualclosedate") or ""
    probability = raw.get("closeprobability") or ""
    stage = raw.get("stepname") or ""
    account_id = raw.get("_parentaccountid_value") or ""
    state = raw.get("statecode") or ""

    content_parts = [f"Opportunity: {name}"]
    if value:
        content_parts.append(f"Value: {value}")
    if close_date:
        content_parts.append(f"Close Date: {close_date}")
    if probability:
        content_parts.append(f"Probability: {probability}%")
    if stage:
        content_parts.append(f"Stage: {stage}")
    if account_id:
        content_parts.append(f"Account ID: {account_id}")
    if state:
        content_parts.append(f"State: {state}")

    source_url = (
        f"{instance_url.rstrip('/')}/main.aspx?etn=opportunity&id={opp_id}"
        if instance_url and opp_id
        else ""
    )

    return ConnectorDocument(
        id=_stable_id("opportunity", opp_id),
        source="dynamics365",
        type="opportunity",
        title=name,
        content="\n".join(content_parts),
        metadata={
            "opportunityid": opp_id,
            "value": str(value) if value else "",
            "close_date": close_date,
            "probability": str(probability) if probability else "",
            "stage": stage,
            "account_id": account_id,
            "state": str(state) if state else "",
            "createdon": raw.get("createdon", ""),
            "modifiedon": raw.get("modifiedon", ""),
        },
        synced_at=_now_iso(),
        source_url=source_url,
    )
