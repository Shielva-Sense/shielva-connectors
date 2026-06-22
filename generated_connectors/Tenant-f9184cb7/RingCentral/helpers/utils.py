"""
RingCentral connector helpers.

- normalize_* functions: raw API dict → ConnectorDocument with stable SHA-256 id
- with_retry: async retry wrapper (skips on AuthError)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Callable, TypeVar

from exceptions import RingCentralAuthError, RingCentralError
from models import ConnectorDocument, ResourceType

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Stable ID helper
# ---------------------------------------------------------------------------


def _stable_id(prefix: str, raw_id: Any) -> str:
    """Return first 16 hex chars of SHA-256(prefix + str(raw_id))."""
    digest = hashlib.sha256(f"{prefix}{raw_id}".encode()).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


def normalize_call_log(raw: dict[str, Any]) -> ConnectorDocument:
    """Normalize a raw RingCentral call-log record."""
    raw_id = raw.get("id", "")
    stable = _stable_id("call_log:", raw_id)

    from_party = raw.get("from", {})
    to_party = raw.get("to", {})

    normalized: dict[str, Any] = {
        "id": stable,
        "raw_id": raw_id,
        "direction": raw.get("direction", ""),
        "type": raw.get("type", ""),
        "start_time": raw.get("startTime", ""),
        "duration": raw.get("duration", 0),
        "result": raw.get("result", ""),
        "from_number": from_party.get("phoneNumber", "") if isinstance(from_party, dict) else "",
        "from_name": from_party.get("name", "") if isinstance(from_party, dict) else "",
        "to_number": to_party.get("phoneNumber", "") if isinstance(to_party, dict) else "",
        "to_name": to_party.get("name", "") if isinstance(to_party, dict) else "",
        "action": raw.get("action", ""),
        "uri": raw.get("uri", ""),
    }

    return ConnectorDocument(
        id=stable,
        resource_type=ResourceType.CALL_LOG,
        raw=raw,
        normalized=normalized,
    )


def normalize_message(raw: dict[str, Any]) -> ConnectorDocument:
    """Normalize a raw RingCentral message record."""
    raw_id = raw.get("id", "")
    stable = _stable_id("message:", raw_id)

    from_party = raw.get("from", {})
    to_parties = raw.get("to", [])

    normalized: dict[str, Any] = {
        "id": stable,
        "raw_id": raw_id,
        "type": raw.get("type", ""),
        "subject": raw.get("subject", ""),
        "direction": raw.get("direction", ""),
        "status": raw.get("readStatus", ""),
        "creation_time": raw.get("creationTime", ""),
        "last_modified_time": raw.get("lastModifiedTime", ""),
        "from_number": from_party.get("phoneNumber", "") if isinstance(from_party, dict) else "",
        "from_name": from_party.get("name", "") if isinstance(from_party, dict) else "",
        "to": [
            {
                "number": p.get("phoneNumber", "") if isinstance(p, dict) else "",
                "name": p.get("name", "") if isinstance(p, dict) else "",
            }
            for p in (to_parties if isinstance(to_parties, list) else [])
        ],
        "message_status": raw.get("messageStatus", ""),
        "conversation_id": raw.get("conversationId", ""),
        "uri": raw.get("uri", ""),
    }

    return ConnectorDocument(
        id=stable,
        resource_type=ResourceType.MESSAGE,
        raw=raw,
        normalized=normalized,
    )


def normalize_extension(raw: dict[str, Any]) -> ConnectorDocument:
    """Normalize a raw RingCentral extension record."""
    raw_id = raw.get("id", "")
    stable = _stable_id("extension:", raw_id)

    contact = raw.get("contact", {})

    normalized: dict[str, Any] = {
        "id": stable,
        "raw_id": raw_id,
        "extension_number": raw.get("extensionNumber", ""),
        "type": raw.get("type", ""),
        "status": raw.get("status", ""),
        "name": raw.get("name", ""),
        "first_name": contact.get("firstName", "") if isinstance(contact, dict) else "",
        "last_name": contact.get("lastName", "") if isinstance(contact, dict) else "",
        "email": contact.get("email", "") if isinstance(contact, dict) else "",
        "department": contact.get("department", "") if isinstance(contact, dict) else "",
        "uri": raw.get("uri", ""),
    }

    return ConnectorDocument(
        id=stable,
        resource_type=ResourceType.EXTENSION,
        raw=raw,
        normalized=normalized,
    )


def normalize_contact(raw: dict[str, Any]) -> ConnectorDocument:
    """Normalize a raw RingCentral address-book contact record."""
    raw_id = raw.get("id", "")
    stable = _stable_id("contact:", raw_id)

    phone_numbers = raw.get("phoneNumbers", [])
    emails = raw.get("emails", [])

    normalized: dict[str, Any] = {
        "id": stable,
        "raw_id": raw_id,
        "first_name": raw.get("firstName", ""),
        "last_name": raw.get("lastName", ""),
        "company": raw.get("company", ""),
        "job_title": raw.get("jobTitle", ""),
        "phone_numbers": [
            {
                "type": p.get("type", "") if isinstance(p, dict) else "",
                "number": p.get("phoneNumber", "") if isinstance(p, dict) else "",
            }
            for p in (phone_numbers if isinstance(phone_numbers, list) else [])
        ],
        "emails": [
            {
                "type": e.get("type", "") if isinstance(e, dict) else "",
                "email": e.get("email", "") if isinstance(e, dict) else "",
            }
            for e in (emails if isinstance(emails, list) else [])
        ],
        "notes": raw.get("notes", ""),
        "uri": raw.get("uri", ""),
    }

    return ConnectorDocument(
        id=stable,
        resource_type=ResourceType.CONTACT,
        raw=raw,
        normalized=normalized,
    )


def normalize_meeting(raw: dict[str, Any]) -> ConnectorDocument:
    """Normalize a raw RingCentral meeting record."""
    raw_id = raw.get("id", "")
    stable = _stable_id("meeting:", raw_id)

    normalized: dict[str, Any] = {
        "id": stable,
        "raw_id": raw_id,
        "topic": raw.get("topic", ""),
        "type": raw.get("meetingType", ""),
        "status": raw.get("status", ""),
        "schedule": raw.get("schedule", {}),
        "start_time": (raw.get("schedule") or {}).get("startTime", ""),
        "duration_in_minutes": (raw.get("schedule") or {}).get("durationInMinutes", 0),
        "timezone": (raw.get("schedule") or {}).get("timeZone", {}).get("name", ""),
        "password": raw.get("password", ""),
        "host": raw.get("host", {}),
        "uri": raw.get("uri", ""),
    }

    return ConnectorDocument(
        id=stable,
        resource_type=ResourceType.MEETING,
        raw=raw,
        normalized=normalized,
    )


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


async def with_retry(
    fn: Callable[..., Any],
    max_attempts: int = 3,
    initial_delay: float = 1.0,
) -> Any:
    """
    Retry an async callable up to max_attempts times with exponential back-off.
    Skips retry immediately on RingCentralAuthError (credentials won't fix themselves).
    """
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except RingCentralAuthError:
            # Auth errors are not retryable — re-raise immediately
            raise
        except RingCentralError as exc:
            last_error = exc
            if attempt < max_attempts:
                delay = initial_delay * (2 ** (attempt - 1))
                logger.warning(
                    "RingCentral request failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
        except Exception as exc:
            last_error = exc
            if attempt < max_attempts:
                delay = initial_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Unexpected error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    raise (last_error or RingCentralError("Max retry attempts exceeded"))
