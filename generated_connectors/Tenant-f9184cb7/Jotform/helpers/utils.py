from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import JotformAuthError, JotformError, JotformRateLimitError
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
    Rate-limit errors honour ``retry_after`` when present.
    """
    last_exc: JotformError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except JotformAuthError:
            raise
        except JotformRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except JotformError as exc:
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


def normalize_form(f: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Jotform form object into a ConnectorDocument.

    source_id = SHA-256("form:" + f["id"])[:16]
    """
    form_id: str = str(f.get("id", ""))
    title: str = f.get("title", "") or f"Form {form_id}"
    status: str = f.get("status", "")
    created_at: str = f.get("created_at", "")
    updated_at: str = f.get("updated_at", "")
    url: str = f.get("url", "")
    count: str = str(f.get("count", ""))

    content_parts: list[str] = [f"Form: {title}"]
    if status:
        content_parts.append(f"Status: {status}")
    if count:
        content_parts.append(f"Submission count: {count}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    source_id = _short_hash(f"form:{form_id}")
    source_url = url or f"https://www.jotform.com/form/{form_id}"

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
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
            "count": count,
        },
    )


def normalize_submission(s: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Jotform submission object into a ConnectorDocument.

    source_id = SHA-256("submission:" + s["id"])[:16]
    """
    submission_id: str = str(s.get("id", ""))
    form_id: str = str(s.get("form_id", ""))
    created_at: str = s.get("created_at", "")
    updated_at: str = s.get("updated_at", "")
    status: str = s.get("status", "")

    # Build content from answers dict
    answers: dict[str, Any] = s.get("answers", {}) or {}
    content_parts: list[str] = []
    for _qid, answer_data in answers.items():
        if not isinstance(answer_data, dict):
            continue
        question: str = answer_data.get("text", "") or answer_data.get("name", "")
        answer: str = _extract_answer(answer_data)
        if question and answer:
            content_parts.append(f"{question}: {answer}")
        elif answer:
            content_parts.append(answer)

    content = "\n\n".join(content_parts) if content_parts else f"Submission {submission_id}"
    title = f"Submission {submission_id}" + (f" (Form {form_id})" if form_id else "")
    source_id = _short_hash(f"submission:{submission_id}")
    source_url = f"https://www.jotform.com/inbox/{form_id}" if form_id else ""

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "type": "submission",
            "submission_id": submission_id,
            "form_id": form_id,
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
            "answer_count": len(content_parts),
        },
    )


def normalize_question(q: dict[str, Any], form_id: str) -> ConnectorDocument:
    """Convert a raw Jotform question object into a ConnectorDocument.

    source_id = SHA-256("question:" + form_id + ":" + q["qid"])[:16]
    """
    qid: str = str(q.get("qid", "") or q.get("order", ""))
    q_type: str = q.get("type", "")
    text: str = q.get("text", "") or q.get("name", "") or f"Question {qid}"
    required: str = str(q.get("required", "No"))

    content_parts: list[str] = [f"Question: {text}", f"Type: {q_type}"]
    if required:
        content_parts.append(f"Required: {required}")

    # Include options if available
    options = q.get("options", "")
    if options:
        content_parts.append(f"Options: {options}")

    source_id = _short_hash(f"question:{form_id}:{qid}")
    source_url = f"https://www.jotform.com/form/{form_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=text,
        content="\n".join(content_parts),
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "type": "question",
            "qid": qid,
            "form_id": form_id,
            "q_type": q_type,
            "required": required,
        },
    )


def _extract_answer(answer_data: dict[str, Any]) -> str:
    """Extract a human-readable string from a Jotform answer dict."""
    # Jotform answers can be nested under "answer" key or directly structured
    answer = answer_data.get("answer")
    if answer is None:
        return ""
    if isinstance(answer, str):
        return answer.strip()
    if isinstance(answer, (int, float, bool)):
        return str(answer)
    if isinstance(answer, list):
        return ", ".join(str(v) for v in answer if v)
    if isinstance(answer, dict):
        # Handle structured answers (name, address, etc.)
        parts = [str(v) for v in answer.values() if v]
        return " ".join(parts)
    return str(answer)
