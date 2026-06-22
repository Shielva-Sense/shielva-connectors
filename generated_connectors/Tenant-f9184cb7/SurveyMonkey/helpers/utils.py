from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import SurveyMonkeyAuthError, SurveyMonkeyError, SurveyMonkeyRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

SURVEYMONKEY_WEB_BASE: str = "https://www.surveymonkey.com"


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
    last_exc: SurveyMonkeyError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except SurveyMonkeyAuthError:
            raise
        except SurveyMonkeyRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except SurveyMonkeyError as exc:
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


def _short_hash(value: str) -> str:
    """Return a 16-character hex digest of SHA-256 for the given string."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def normalize_survey(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw SurveyMonkey survey into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of ``survey:{survey_id}`` so it
    is stable and collision-resistant within Shielva's 16-char source_id budget.
    """
    survey_id: str = str(raw.get("id", ""))
    title: str = raw.get("title", "") or f"Survey {survey_id}"
    href: str = raw.get("href", "")
    created_at: str = raw.get("date_created", "")
    modified_at: str = raw.get("date_modified", "")
    question_count: int = raw.get("question_count", 0) or 0
    page_count: int = raw.get("page_count", 0) or 0
    response_count: int = raw.get("response_count", 0) or 0

    content_parts: list[str] = [
        f"Survey: {title}",
        f"Questions: {question_count}",
        f"Pages: {page_count}",
        f"Responses: {response_count}",
    ]
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if modified_at:
        content_parts.append(f"Last modified: {modified_at}")

    source_id = _short_hash(f"survey:{survey_id}")
    source_url = href or f"{SURVEYMONKEY_WEB_BASE}/r/{survey_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        resource_kind="survey",
        source_url=source_url,
        metadata={
            "survey_id": survey_id,
            "title": title,
            "date_created": created_at,
            "date_modified": modified_at,
            "question_count": question_count,
            "page_count": page_count,
            "response_count": response_count,
        },
    )


def normalize_response(
    raw: dict[str, Any],
    survey_id: str,
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw SurveyMonkey survey response into a ConnectorDocument.

    The source_id is a 16-char SHA-256 prefix of ``response:{response_id}`` for
    deterministic, collision-resistant identity within Shielva.
    """
    response_id: str = str(raw.get("id", ""))
    date_created: str = raw.get("date_created", "")
    date_modified: str = raw.get("date_modified", "")
    ip_address: str = raw.get("ip_address", "")
    response_status: str = raw.get("response_status", "")
    total_time: int = raw.get("total_time", 0) or 0
    collector_id: str = str(raw.get("collector_id", ""))

    pages: list[dict[str, Any]] = raw.get("pages", []) or []
    content_parts: list[str] = []
    for page in pages:
        questions: list[dict[str, Any]] = page.get("questions", []) or []
        for question in questions:
            question_id: str = str(question.get("id", ""))
            answers: list[dict[str, Any]] = question.get("answers", []) or []
            for answer in answers:
                text: str = _extract_answer_text(answer)
                if text:
                    content_parts.append(f"[Q:{question_id}]: {text}")

    content = (
        "\n\n".join(content_parts)
        if content_parts
        else f"Survey response {response_id} (no answers)"
    )
    source_id = _short_hash(f"response:{response_id}")
    title = f"Response {response_id[:8] if response_id else 'unknown'} — Survey {survey_id}"
    source_url = (
        f"{SURVEYMONKEY_WEB_BASE}/analyze/survey/{survey_id}/respondent/{response_id}"
    )

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        resource_kind="response",
        source_url=source_url,
        metadata={
            "response_id": response_id,
            "survey_id": survey_id,
            "collector_id": collector_id,
            "date_created": date_created,
            "date_modified": date_modified,
            "ip_address": ip_address,
            "response_status": response_status,
            "total_time": total_time,
            "page_count": len(pages),
        },
    )


def normalize_collector(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a raw SurveyMonkey collector into a ConnectorDocument."""
    collector_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "") or f"Collector {collector_id}"
    status: str = raw.get("status", "")
    collector_type: str = raw.get("type", "")
    href: str = raw.get("href", "")
    date_created: str = raw.get("date_created", "")
    date_modified: str = raw.get("date_modified", "")
    survey_id: str = str(raw.get("survey_id", ""))

    content_parts: list[str] = [
        f"Collector: {name}",
        f"Type: {collector_type}",
        f"Status: {status}",
    ]
    if date_created:
        content_parts.append(f"Created: {date_created}")
    if date_modified:
        content_parts.append(f"Last modified: {date_modified}")

    source_id = _short_hash(f"collector:{collector_id}")
    source_url = href or f"{SURVEYMONKEY_WEB_BASE}/collect/details/{collector_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=name,
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        resource_kind="collector",
        source_url=source_url,
        metadata={
            "collector_id": collector_id,
            "survey_id": survey_id,
            "name": name,
            "status": status,
            "type": collector_type,
            "date_created": date_created,
            "date_modified": date_modified,
        },
    )


def _extract_answer_text(answer: dict[str, Any]) -> str:
    """Extract a human-readable string from a SurveyMonkey answer object."""
    # choice_id based answers
    if "text" in answer and answer["text"]:
        return str(answer["text"])
    if "row_id" in answer:
        row_id = answer.get("row_id", "")
        choice_id = answer.get("choice_id", "")
        if row_id or choice_id:
            return f"row={row_id} choice={choice_id}".strip()
    if "choice_id" in answer:
        return f"choice={answer['choice_id']}"
    if "other_id" in answer:
        text = answer.get("text", "") or f"other={answer['other_id']}"
        return str(text)
    return ""
