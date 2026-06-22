from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import LeverAuthError, LeverError, LeverRateLimitError
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
    Rate-limit errors honour the retry_after attribute when present.
    """
    last_exc: LeverError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except LeverAuthError:
            raise
        except LeverRateLimitError as exc:
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
        except LeverError as exc:
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


def normalize_opportunity(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Lever opportunity into a ConnectorDocument.

    source_id = sha256("opportunity:{id}")[:16]
    """
    opp_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "") or f"Candidate {opp_id}"
    headline: str = raw.get("headline", "") or ""

    # Contact info
    emails: list[str] = raw.get("emails", []) or []
    phones_raw: list[Any] = raw.get("phones", []) or []
    phones: list[str] = [
        p.get("value", "") if isinstance(p, dict) else str(p)
        for p in phones_raw
    ]

    # Stage and owner
    stage_raw: Any = raw.get("stage", {}) or {}
    stage_text: str = (
        stage_raw.get("text", "") if isinstance(stage_raw, dict) else str(stage_raw)
    )
    owner_raw: Any = raw.get("owner", {}) or {}
    owner_name: str = (
        owner_raw.get("name", "") if isinstance(owner_raw, dict) else str(owner_raw)
    )

    # Posting link
    posting_raw: Any = raw.get("posting", "") or ""
    posting_id: str = (
        posting_raw.get("id", "") if isinstance(posting_raw, dict) else str(posting_raw)
    )

    tags: list[str] = raw.get("tags", []) or []
    archived: bool = bool(raw.get("archived"))
    created_at: int = int(raw.get("createdAt", 0) or 0)
    updated_at: int = int(raw.get("updatedAt", 0) or 0)

    content_parts: list[str] = [f"Candidate: {name}"]
    if headline:
        content_parts.append(f"Headline: {headline}")
    if stage_text:
        content_parts.append(f"Stage: {stage_text}")
    if owner_name:
        content_parts.append(f"Owner: {owner_name}")
    if emails:
        content_parts.append(f"Email: {', '.join(emails)}")
    if phones:
        content_parts.append(f"Phone: {', '.join(p for p in phones if p)}")
    if posting_id:
        content_parts.append(f"Posting ID: {posting_id}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    content_parts.append(f"Archived: {archived}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"opportunity:{opp_id}")
    title = f"Opportunity: {name}"
    source_url = f"https://hire.lever.co/candidates/{opp_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "opportunity_id": opp_id,
            "name": name,
            "headline": headline,
            "stage": stage_text,
            "owner": owner_name,
            "emails": emails,
            "phones": [p for p in phones if p],
            "posting_id": posting_id,
            "tags": tags,
            "archived": archived,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )


def normalize_posting(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Lever posting into a ConnectorDocument.

    source_id = sha256("posting:{id}")[:16]
    """
    posting_id: str = str(raw.get("id", ""))
    text: str = raw.get("text", "") or f"Posting {posting_id}"
    state: str = raw.get("state", "") or ""

    # Categories
    categories_raw: Any = raw.get("categories", {}) or {}
    department: str = ""
    team: str = ""
    location: str = ""
    if isinstance(categories_raw, dict):
        department = categories_raw.get("department", "") or ""
        team = categories_raw.get("team", "") or ""
        location = categories_raw.get("location", "") or ""

    tags: list[str] = raw.get("tags", []) or []
    created_at: int = int(raw.get("createdAt", 0) or 0)
    updated_at: int = int(raw.get("updatedAt", 0) or 0)

    # URLs
    urls_raw: Any = raw.get("urls", {}) or {}
    list_url: str = ""
    show_url: str = ""
    apply_url: str = ""
    if isinstance(urls_raw, dict):
        list_url = urls_raw.get("list", "") or ""
        show_url = urls_raw.get("show", "") or ""
        apply_url = urls_raw.get("apply", "") or ""

    content_parts: list[str] = [f"Job: {text}"]
    if state:
        content_parts.append(f"State: {state}")
    if department:
        content_parts.append(f"Department: {department}")
    if team:
        content_parts.append(f"Team: {team}")
    if location:
        content_parts.append(f"Location: {location}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    if apply_url:
        content_parts.append(f"Apply URL: {apply_url}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"posting:{posting_id}")
    title = f"Job Posting: {text}"
    source_url = show_url or list_url or f"https://hire.lever.co/jobs/{posting_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "posting_id": posting_id,
            "text": text,
            "state": state,
            "department": department,
            "team": team,
            "location": location,
            "tags": tags,
            "created_at": created_at,
            "updated_at": updated_at,
            "urls": {
                "list": list_url,
                "show": show_url,
                "apply": apply_url,
            },
        },
    )


def normalize_user(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Lever user into a ConnectorDocument.

    source_id = sha256("user:{id}")[:16]
    """
    user_id: str = str(raw.get("id", ""))
    name: str = raw.get("name", "") or f"User {user_id}"
    email: str = raw.get("email", "") or ""
    username: str = raw.get("username", "") or ""
    access_role: str = raw.get("accessRole", "") or raw.get("access_role", "") or ""
    active: bool = bool(raw.get("active", True))
    created_at: int = int(raw.get("createdAt", 0) or 0)

    content_parts: list[str] = [f"User: {name}"]
    if email:
        content_parts.append(f"Email: {email}")
    if username:
        content_parts.append(f"Username: {username}")
    if access_role:
        content_parts.append(f"Role: {access_role}")
    content_parts.append(f"Active: {active}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"user:{user_id}")
    title = f"User: {name}"
    source_url = f"https://hire.lever.co/settings/team/{user_id}"

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "user_id": user_id,
            "name": name,
            "email": email,
            "username": username,
            "access_role": access_role,
            "active": active,
            "created_at": created_at,
        },
    )


def normalize_interview(raw: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Lever interview into a ConnectorDocument.

    source_id = sha256("interview:{id}")[:16]
    """
    interview_id: str = str(raw.get("id", ""))
    subject: str = raw.get("subject", "") or f"Interview {interview_id}"
    note: str = raw.get("note", "") or ""
    date: int = int(raw.get("date", 0) or 0)
    duration: int = int(raw.get("duration", 0) or 0)
    location: str = raw.get("location", "") or ""
    canceled: bool = bool(raw.get("canceled", False))

    # Interviewers
    interviewers_raw: list[Any] = raw.get("interviewers", []) or []
    interviewer_names: list[str] = []
    for iv in interviewers_raw:
        if isinstance(iv, dict):
            iv_name = iv.get("name", "") or iv.get("email", "")
            if iv_name:
                interviewer_names.append(iv_name)

    # Opportunity link
    opp_raw: Any = raw.get("opportunity", "") or ""
    opp_id: str = (
        opp_raw.get("id", "") if isinstance(opp_raw, dict) else str(opp_raw)
    )

    content_parts: list[str] = [f"Interview: {subject}"]
    if location:
        content_parts.append(f"Location: {location}")
    if duration:
        content_parts.append(f"Duration: {duration} minutes")
    if interviewer_names:
        content_parts.append(f"Interviewers: {', '.join(interviewer_names)}")
    if opp_id:
        content_parts.append(f"Opportunity ID: {opp_id}")
    if note:
        content_parts.append(f"Note: {note}")
    content_parts.append(f"Canceled: {canceled}")

    content = "\n".join(content_parts)
    source_id = _short_hash(f"interview:{interview_id}")
    title = f"Interview: {subject}"
    source_url = (
        f"https://hire.lever.co/candidates/{opp_id}/interview/{interview_id}"
        if opp_id
        else f"https://hire.lever.co/interviews/{interview_id}"
    )

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id="",
        tenant_id="",
        source_url=source_url,
        metadata={
            "interview_id": interview_id,
            "subject": subject,
            "date": date,
            "duration": duration,
            "location": location,
            "canceled": canceled,
            "interviewers": interviewer_names,
            "opportunity_id": opp_id,
        },
    )
