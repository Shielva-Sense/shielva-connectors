from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import DocuSignAuthError, DocuSignError, DocuSignRateLimitError
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

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: DocuSignError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except DocuSignAuthError:
            raise
        except DocuSignRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except DocuSignError as exc:
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


def _stable_id(envelope_id: str) -> str:
    """Return a 16-char stable ID derived from SHA-256(envelope_id)."""
    return hashlib.sha256(envelope_id.encode()).hexdigest()[:16]


def normalize_envelope(
    envelope: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a raw DocuSign Envelope object into a ConnectorDocument."""
    envelope_id = envelope.get("envelopeId", "")
    status = envelope.get("status", "unknown")
    subject = envelope.get("emailSubject", "") or f"Envelope {envelope_id}"
    sender = envelope.get("sender", {})
    sender_name = sender.get("userName", "") or sender.get("email", "unknown")
    sent_date = envelope.get("sentDateTime", "")
    completed_date = envelope.get("completedDateTime", "")
    created_date = envelope.get("createdDateTime", "")
    recipients_uri = envelope.get("recipientsUri", "")

    title = f"DocuSign Envelope: {subject} [{status}]"
    content_parts = [
        f"Envelope ID: {envelope_id}",
        f"Subject: {subject}",
        f"Status: {status}",
        f"Sender: {sender_name}",
    ]
    if sent_date:
        content_parts.append(f"Sent: {sent_date}")
    if completed_date:
        content_parts.append(f"Completed: {completed_date}")
    if created_date:
        content_parts.append(f"Created: {created_date}")

    return ConnectorDocument(
        source_id=_stable_id(envelope_id),
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.docusign.com/documents/details/{envelope_id}",
        metadata={
            "envelope_id": envelope_id,
            "status": status,
            "subject": subject,
            "sender": sender_name,
            "sent_date": sent_date,
            "completed_date": completed_date,
            "created_date": created_date,
            "recipients_uri": recipients_uri,
        },
    )
