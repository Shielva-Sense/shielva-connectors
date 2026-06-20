from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import TypeformAuthError, TypeformError, TypeformRateLimitError
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
    """
    last_exc: TypeformError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except TypeformAuthError:
            raise
        except TypeformRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except TypeformError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _short_hash(value: str) -> str:
    """Return a 16-character hex digest of SHA-256 for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def normalize_form(form: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Typeform form object into a ConnectorDocument.

    source_id = SHA-256("form:" + form_id)[:16]
    type = "form"
    """
    form_id: str = str(form.get("id", ""))
    title: str = form.get("title", "") or f"Form {form_id}"
    form_type: str = form.get("type", "")
    created_at: str = form.get("created_at", "")
    last_updated_at: str = form.get("last_updated_at", "")

    # Collect field titles for content
    fields: list[dict[str, Any]] = form.get("fields", []) or []
    field_titles: list[str] = [f.get("title", "") for f in fields if f.get("title")]

    content_parts: list[str] = [f"Form: {title}"]
    if form_type:
        content_parts.append(f"Type: {form_type}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if last_updated_at:
        content_parts.append(f"Last updated: {last_updated_at}")
    if field_titles:
        content_parts.append(f"Fields: {', '.join(field_titles)}")

    source_id = _short_hash(f"form:{form_id}")
    source_url = f"https://admin.typeform.com/form/{form_id}/create"

    # Try canonical display link if available
    links: dict[str, Any] = form.get("links", {}) or {}
    if links.get("display"):
        source_url = str(links["display"])

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "type": "form",
            "form_id": form_id,
            "form_type": form_type,
            "created_at": created_at,
            "last_updated_at": last_updated_at,
            "field_count": len(fields),
        },
    )


def normalize_response(
    response: dict[str, Any],
    form_id: str,
) -> ConnectorDocument:
    """Convert a raw Typeform response object into a ConnectorDocument.

    source_id = SHA-256("response:" + response_token)[:16]
    type = "form_response"
    """
    response_token: str = response.get("token", "")
    submitted_at: str = response.get("submitted_at", "")
    landed_at: str = response.get("landed_at", "")

    # Build content from each answer
    answers: list[dict[str, Any]] = response.get("answers", []) or []
    content_parts: list[str] = []
    for answer in answers:
        field_ref: str = (
            answer.get("field", {}).get("ref", "")
            or answer.get("field", {}).get("id", "")
        )
        field_type: str = answer.get("type", "")
        value: str = _extract_answer_value(answer, field_type)
        if value:
            content_parts.append(f"[{field_ref}]: {value}")

    content = (
        "\n\n".join(content_parts)
        if content_parts
        else f"Response to form {form_id}"
    )

    source_id = _short_hash(f"response:{response_token}")
    title = f"Form {form_id} — Response {response_token[:8] if response_token else 'unknown'}"
    source_url = f"https://admin.typeform.com/form/{form_id}/results"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "type": "form_response",
            "form_id": form_id,
            "response_token": response_token,
            "submitted_at": submitted_at,
            "landed_at": landed_at,
            "answer_count": len(answers),
        },
    )


def _extract_answer_value(answer: dict[str, Any], field_type: str) -> str:
    """Extract a string representation of a Typeform answer value."""
    if field_type == "text":
        return str(answer.get("text", ""))
    if field_type == "email":
        return str(answer.get("email", ""))
    if field_type == "url":
        return str(answer.get("url", ""))
    if field_type == "number":
        val = answer.get("number")
        return str(val) if val is not None else ""
    if field_type == "boolean":
        val = answer.get("boolean")
        return str(val) if val is not None else ""
    if field_type == "date":
        return str(answer.get("date", ""))
    if field_type == "choice":
        choice = answer.get("choice", {})
        return choice.get("label", "") or choice.get("other", "")
    if field_type == "choices":
        choices = answer.get("choices", {})
        labels: list[str] = choices.get("labels", []) or []
        other: str = choices.get("other", "") or ""
        parts = labels + ([other] if other else [])
        return ", ".join(parts)
    if field_type == "file_url":
        return str(answer.get("file_url", ""))
    if field_type == "payment":
        payment = answer.get("payment", {})
        return f"{payment.get('amount', '')} {payment.get('currency', '')}".strip()
    # Fallback: try common answer value fields
    for key in ("text", "email", "url", "number", "boolean", "date"):
        if key in answer:
            return str(answer[key])
    return ""
