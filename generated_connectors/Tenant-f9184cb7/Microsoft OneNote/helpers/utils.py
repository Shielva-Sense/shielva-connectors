"""Retry helper + XHTML page builders + ISO-8601 parsing for Microsoft Graph OneNote."""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from html import escape as _html_escape
from typing import Any, Callable, Coroutine, Optional

import httpx
import structlog

from exceptions import OneNoteNetworkError, OneNoteRateLimitError

logger = structlog.get_logger(__name__)

RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> Any:
    """Execute *coro_fn()* with exponential-backoff retry on transient errors.

    Retries on :class:`OneNoteRateLimitError` (honouring the server-supplied
    ``retry_after`` on the first attempt), :class:`OneNoteNetworkError`, and
    httpx ``RequestError``. All other exceptions surface immediately.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except OneNoteRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = (
                exc.retry_after
                if (exc.retry_after and attempt == 0)
                else min(
                    base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                    max_delay,
                )
            )
            logger.warning(
                "onenote.rate_limit — retrying",
                attempt=attempt + 1, delay=delay,
            )
            await asyncio.sleep(delay)
        except (OneNoteNetworkError, httpx.RequestError) as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "onenote.transient_error — retrying",
                attempt=attempt + 1, delay=delay, error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def xhtml_escape(value: str) -> str:
    """HTML-escape a string for safe inclusion in OneNote XHTML page bodies."""
    return _html_escape(value or "", quote=True)


def build_simple_page_xhtml(title: str, body_html: str) -> str:
    """Build a minimal valid OneNote XHTML page body.

    Useful for callers who want to create a page without hand-rolling the
    ``<html><head><title>…</title></head><body>…</body></html>`` envelope.
    """
    safe_title = xhtml_escape(title or "Untitled")
    return (
        "<!DOCTYPE html>"
        "<html>"
        f"<head><title>{safe_title}</title></head>"
        f"<body>{body_html or ''}</body>"
        "</html>"
    )


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (Microsoft Graph returns RFC 3339 with Z)."""
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
