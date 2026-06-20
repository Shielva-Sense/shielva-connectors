from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import PandaDocAuthError, PandaDocError, PandaDocRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

PANDADOC_APP_BASE = "https://app.pandadoc.com"


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the retry_after field when present.
    """
    last_exc: PandaDocError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except PandaDocAuthError:
            raise
        except PandaDocRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except PandaDocError as exc:
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


def _stable_id(prefix: str, resource_id: str) -> str:
    """Return a 16-char stable ID: SHA-256('<prefix>:<resource_id>')[:16]."""
    raw = f"{prefix}:{resource_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_document(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw PandaDoc document API response into a ConnectorDocument."""
    doc_id: str = raw.get("id", "")
    name: str = raw.get("name", "") or f"Document {doc_id}"
    status: str = raw.get("status", "unknown")
    created_at: str = raw.get("date_created", "") or raw.get("created_at", "")
    modified_at: str = raw.get("date_modified", "") or raw.get("modified_at", "")
    expiration_date: str = raw.get("expiration_date", "")
    created_by: dict[str, Any] = raw.get("created_by", {})
    creator_name: str = (
        created_by.get("firstName", "")
        + " "
        + created_by.get("lastName", "")
    ).strip() or created_by.get("email", "unknown")

    template_uuid: str = raw.get("template_uuid", "")
    recipients: list[dict[str, Any]] = raw.get("recipients", [])
    recipient_emails: list[str] = [r.get("email", "") for r in recipients if r.get("email")]

    title = f"PandaDoc Document: {name} [{status}]"
    content_parts = [
        f"Document ID: {doc_id}",
        f"Name: {name}",
        f"Status: {status}",
        f"Created by: {creator_name}",
    ]
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if modified_at:
        content_parts.append(f"Modified: {modified_at}")
    if expiration_date:
        content_parts.append(f"Expires: {expiration_date}")
    if template_uuid:
        content_parts.append(f"Template: {template_uuid}")
    if recipient_emails:
        content_parts.append(f"Recipients: {', '.join(recipient_emails)}")

    return ConnectorDocument(
        source_id=_stable_id("document", doc_id),
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{PANDADOC_APP_BASE}/document/{doc_id}",
        resource_type="document",
        metadata={
            "document_id": doc_id,
            "name": name,
            "status": status,
            "created_by": creator_name,
            "created_at": created_at,
            "modified_at": modified_at,
            "expiration_date": expiration_date,
            "template_uuid": template_uuid,
            "recipient_emails": recipient_emails,
        },
    )


def normalize_template(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw PandaDoc template API response into a ConnectorDocument."""
    tpl_id: str = raw.get("id", "")
    name: str = raw.get("name", "") or f"Template {tpl_id}"
    created_at: str = raw.get("date_created", "") or raw.get("created_at", "")
    modified_at: str = raw.get("date_modified", "") or raw.get("modified_at", "")
    created_by: dict[str, Any] = raw.get("created_by", {})
    creator_name: str = (
        created_by.get("firstName", "")
        + " "
        + created_by.get("lastName", "")
    ).strip() or created_by.get("email", "unknown")

    title = f"PandaDoc Template: {name}"
    content_parts = [
        f"Template ID: {tpl_id}",
        f"Name: {name}",
        f"Created by: {creator_name}",
    ]
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if modified_at:
        content_parts.append(f"Modified: {modified_at}")

    return ConnectorDocument(
        source_id=_stable_id("template", tpl_id),
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{PANDADOC_APP_BASE}/template/{tpl_id}",
        resource_type="template",
        metadata={
            "template_id": tpl_id,
            "name": name,
            "created_by": creator_name,
            "created_at": created_at,
            "modified_at": modified_at,
        },
    )


def normalize_contact(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw PandaDoc contact API response into a ConnectorDocument."""
    contact_id: str = raw.get("id", "")
    first_name: str = raw.get("first_name", "")
    last_name: str = raw.get("last_name", "")
    full_name: str = f"{first_name} {last_name}".strip() or contact_id
    email: str = raw.get("email", "")
    company: str = raw.get("company", "")
    job_title: str = raw.get("job_title", "")
    phone: str = raw.get("phone", "")
    created_at: str = raw.get("date_created", "") or raw.get("created_at", "")

    title = f"PandaDoc Contact: {full_name}"
    content_parts = [
        f"Contact ID: {contact_id}",
        f"Name: {full_name}",
    ]
    if email:
        content_parts.append(f"Email: {email}")
    if company:
        content_parts.append(f"Company: {company}")
    if job_title:
        content_parts.append(f"Job Title: {job_title}")
    if phone:
        content_parts.append(f"Phone: {phone}")
    if created_at:
        content_parts.append(f"Created: {created_at}")

    return ConnectorDocument(
        source_id=_stable_id("contact", contact_id),
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{PANDADOC_APP_BASE}/contacts/{contact_id}",
        resource_type="contact",
        metadata={
            "contact_id": contact_id,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "company": company,
            "job_title": job_title,
            "phone": phone,
            "created_at": created_at,
        },
    )


def normalize_form(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw PandaDoc form API response into a ConnectorDocument."""
    form_id: str = raw.get("id", "")
    name: str = raw.get("name", "") or f"Form {form_id}"
    status: str = raw.get("status", "unknown")
    created_at: str = raw.get("date_created", "") or raw.get("created_at", "")
    modified_at: str = raw.get("date_modified", "") or raw.get("modified_at", "")

    title = f"PandaDoc Form: {name} [{status}]"
    content_parts = [
        f"Form ID: {form_id}",
        f"Name: {name}",
        f"Status: {status}",
    ]
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if modified_at:
        content_parts.append(f"Modified: {modified_at}")

    return ConnectorDocument(
        source_id=_stable_id("form", form_id),
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=f"{PANDADOC_APP_BASE}/form/{form_id}",
        resource_type="form",
        metadata={
            "form_id": form_id,
            "name": name,
            "status": status,
            "created_at": created_at,
            "modified_at": modified_at,
        },
    )
