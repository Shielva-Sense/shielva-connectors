from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import OutreachAuthError, OutreachError, OutreachRateLimitError
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
    Rate-limit errors honour the retry_after value when present.
    """
    last_exc: OutreachError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except OutreachAuthError:
            raise
        except OutreachRateLimitError as exc:
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
        except OutreachError as exc:
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


def _get_attrs(raw: dict[str, Any]) -> dict[str, Any]:
    """Unwrap JSON:API attributes — fall back to the raw dict if absent."""
    return raw.get("attributes") or raw


def normalize_prospect(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Outreach JSON:API prospect into a ConnectorDocument.

    Outreach wraps field values inside ``attributes`` — this function unwraps
    them so callers do not need to know the JSON:API envelope.

    The source_id is a 16-char SHA-256 prefix of ``prospect:{id}`` making it
    deterministic and collision-resistant.
    """
    prospect_id: int | str = raw.get("id", 0)
    attrs: dict[str, Any] = _get_attrs(raw)

    first_name: str = attrs.get("firstName", "") or ""
    last_name: str = attrs.get("lastName", "") or ""
    email: str = attrs.get("emails", [None])[0] if attrs.get("emails") else ""
    if isinstance(email, dict):
        email = email.get("email", "") or ""
    title: str = attrs.get("title", "") or ""
    company: str = attrs.get("company", attrs.get("accountId", "")) or ""
    phone: str = attrs.get("phones", [None])[0] if attrs.get("phones") else ""
    if isinstance(phone, dict):
        phone = phone.get("phone", "") or ""
    stage: str = attrs.get("stage", "") or ""
    created_at: Any = attrs.get("createdAt", attrs.get("created_at"))
    updated_at: Any = attrs.get("updatedAt", attrs.get("updated_at"))

    full_name = f"{first_name} {last_name}".strip()
    display_name = full_name or str(email) or f"Prospect {prospect_id}"
    doc_title = f"Prospect: {display_name}"

    parts: list[str] = []
    if full_name:
        parts.append(f"Name: {full_name}")
    if email:
        parts.append(f"Email: {email}")
    if title:
        parts.append(f"Title: {title}")
    if company:
        parts.append(f"Company: {company}")
    if phone:
        parts.append(f"Phone: {phone}")
    if stage:
        parts.append(f"Stage: {stage}")

    content = "\n".join(parts) if parts else display_name
    source_id = _short_hash(f"outreach_prospect:{prospect_id}")
    source_url = f"https://app.outreach.io/prospects/{prospect_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=doc_title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "prospect_id": prospect_id,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "title": title,
            "company": company,
            "phone": phone,
            "stage": stage,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_sequence(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Outreach JSON:API sequence into a ConnectorDocument."""
    sequence_id: int | str = raw.get("id", 0)
    attrs: dict[str, Any] = _get_attrs(raw)

    name: str = attrs.get("name", "") or ""
    description: str = attrs.get("description", "") or ""
    enabled: bool = bool(attrs.get("enabled", False))
    sequence_type: str = attrs.get("sequenceType", "") or ""
    step_count: int = int(attrs.get("stepCount", 0) or 0)
    created_at: Any = attrs.get("createdAt", attrs.get("created_at"))
    updated_at: Any = attrs.get("updatedAt", attrs.get("updated_at"))

    display_name = name or f"Sequence {sequence_id}"
    doc_title = f"Sequence: {display_name}"

    parts: list[str] = []
    if name:
        parts.append(f"Name: {name}")
    if description:
        parts.append(f"Description: {description}")
    if sequence_type:
        parts.append(f"Type: {sequence_type}")
    parts.append(f"Enabled: {enabled}")
    parts.append(f"Steps: {step_count}")

    content = "\n".join(parts) if parts else display_name
    source_id = _short_hash(f"outreach_sequence:{sequence_id}")
    source_url = f"https://app.outreach.io/sequences/{sequence_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=doc_title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "sequence_id": sequence_id,
            "name": name,
            "description": description,
            "enabled": enabled,
            "sequence_type": sequence_type,
            "step_count": step_count,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_account(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw Outreach JSON:API account into a ConnectorDocument."""
    account_id: int | str = raw.get("id", 0)
    attrs: dict[str, Any] = _get_attrs(raw)

    name: str = attrs.get("name", "") or ""
    domain: str = attrs.get("domain", "") or ""
    website: str = attrs.get("websiteUrl", attrs.get("website", "")) or ""
    industry: str = attrs.get("industry", "") or ""
    created_at: Any = attrs.get("createdAt", attrs.get("created_at"))
    updated_at: Any = attrs.get("updatedAt", attrs.get("updated_at"))

    display_name = name or domain or f"Account {account_id}"
    doc_title = f"Account: {display_name}"

    parts: list[str] = []
    if name:
        parts.append(f"Name: {name}")
    if domain:
        parts.append(f"Domain: {domain}")
    if website:
        parts.append(f"Website: {website}")
    if industry:
        parts.append(f"Industry: {industry}")

    content = "\n".join(parts) if parts else display_name
    source_id = _short_hash(f"outreach_account:{account_id}")
    source_url = f"https://app.outreach.io/accounts/{account_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=doc_title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=source_url,
        metadata={
            "account_id": account_id,
            "name": name,
            "domain": domain,
            "website": website,
            "industry": industry,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
