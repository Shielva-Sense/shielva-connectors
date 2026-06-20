"""Copper CRM connector utilities: normalization and retry logic."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Awaitable, Callable, TypeVar

from ..exceptions import CopperAuthError, CopperError
from ..models import ConnectorDocument, ResourceType

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Stable ID helpers
# ---------------------------------------------------------------------------


def _stable_id(prefix: str, record_id: Any) -> str:
    """Return a 16-character hex ID stable for the given prefix + record_id pair."""
    raw = f"{prefix}:{record_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


def normalize_person(raw: dict[str, Any]) -> ConnectorDocument:
    """Normalise a raw Copper People record into a ConnectorDocument."""
    record_id = raw.get("id", "")
    stable = _stable_id("person", record_id)
    name = raw.get("name") or ""
    emails: list[dict[str, str]] = raw.get("emails") or []
    primary_email = emails[0].get("email", "") if emails else ""

    return ConnectorDocument(
        id=stable,
        resource_type=ResourceType.PERSON.value,
        display_name=name,
        raw=raw,
        metadata={
            "copper_id": record_id,
            "primary_email": primary_email,
            "company_id": raw.get("company_id"),
            "company_name": raw.get("company_name"),
            "title": raw.get("title"),
            "tags": raw.get("tags") or [],
            "date_created": raw.get("date_created"),
            "date_modified": raw.get("date_modified"),
        },
    )


def normalize_company(raw: dict[str, Any]) -> ConnectorDocument:
    """Normalise a raw Copper Companies record into a ConnectorDocument."""
    record_id = raw.get("id", "")
    stable = _stable_id("company", record_id)
    name = raw.get("name") or ""

    return ConnectorDocument(
        id=stable,
        resource_type=ResourceType.COMPANY.value,
        display_name=name,
        raw=raw,
        metadata={
            "copper_id": record_id,
            "email_domain": raw.get("email_domain"),
            "phone_numbers": raw.get("phone_numbers") or [],
            "tags": raw.get("tags") or [],
            "date_created": raw.get("date_created"),
            "date_modified": raw.get("date_modified"),
        },
    )


def normalize_opportunity(raw: dict[str, Any]) -> ConnectorDocument:
    """Normalise a raw Copper Opportunities record into a ConnectorDocument."""
    record_id = raw.get("id", "")
    stable = _stable_id("opportunity", record_id)
    name = raw.get("name") or ""

    return ConnectorDocument(
        id=stable,
        resource_type=ResourceType.OPPORTUNITY.value,
        display_name=name,
        raw=raw,
        metadata={
            "copper_id": record_id,
            "status": raw.get("status"),
            "monetary_value": raw.get("monetary_value"),
            "company_id": raw.get("company_id"),
            "company_name": raw.get("company_name"),
            "assignee_id": raw.get("assignee_id"),
            "close_date": raw.get("close_date"),
            "tags": raw.get("tags") or [],
            "date_created": raw.get("date_created"),
            "date_modified": raw.get("date_modified"),
        },
    )


def normalize_task(raw: dict[str, Any]) -> ConnectorDocument:
    """Normalise a raw Copper Tasks record into a ConnectorDocument."""
    record_id = raw.get("id", "")
    stable = _stable_id("task", record_id)
    name = raw.get("name") or ""

    return ConnectorDocument(
        id=stable,
        resource_type=ResourceType.TASK.value,
        display_name=name,
        raw=raw,
        metadata={
            "copper_id": record_id,
            "status": raw.get("status"),
            "due_date": raw.get("due_date"),
            "reminder_date": raw.get("reminder_date"),
            "assignee_id": raw.get("assignee_id"),
            "tags": raw.get("tags") or [],
            "date_created": raw.get("date_created"),
            "date_modified": raw.get("date_modified"),
        },
    )


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> T:
    """Retry an async callable up to *max_attempts* times.

    Skips retries on :class:`CopperAuthError` (no point retrying bad creds).
    Uses exponential back-off: delay = base_delay * 2^attempt.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except CopperAuthError:
            # Auth errors are not transient — never retry.
            raise
        except CopperError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "CopperError on attempt %d/%d (%s) — retrying in %.1fs",
                    attempt + 1,
                    max_attempts,
                    exc.message,
                    delay,
                )
                await asyncio.sleep(delay)
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Unexpected error on attempt %d/%d — retrying in %.1fs",
                    attempt + 1,
                    max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)

    raise last_exc  # type: ignore[misc]
