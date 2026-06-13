"""Shared utility functions for the Gmail connector."""
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from exceptions import GmailAPIError, GmailRateLimitError

logger = structlog.get_logger(__name__)


def parse_gmail_date(header_value: Optional[str]) -> Optional[datetime]:
    """Parse an RFC 2822 Date header string into a UTC datetime."""
    if not header_value:
        return None
    try:
        dt = parsedate_to_datetime(header_value)
        return dt.astimezone(datetime.utcnow().astimezone().tzinfo).replace(tzinfo=None)
    except Exception:
        logger.warning("gmail.parse_date.failed", value=header_value)
        return None


def extract_header(headers: List[Dict[str, str]], name: str) -> Optional[str]:
    """Return the value of the first header whose name matches (case-insensitive)."""
    name_lower = name.lower()
    for header in headers:
        if header.get("name", "").lower() == name_lower:
            return header.get("value")
    return None


def truncate_preview(text: str, max_chars: int = 200) -> str:
    """Truncate body preview text to max_chars characters."""
    if not text:
        return ""
    return text[:max_chars]


def build_after_query(since: datetime) -> str:
    """Build a Gmail search query string for messages after the given datetime."""
    epoch = int(since.timestamp())
    return f"after:{epoch}"


def make_retry_decorator(
    *,
    max_attempts: int = 3,
    initial_wait: float = 1.0,
    multiplier: float = 2.0,
    max_wait: float = 60.0,
) -> Any:
    """Return a tenacity retry decorator for transient API/rate-limit errors."""
    return retry(
        retry=retry_if_exception_type((GmailAPIError, GmailRateLimitError)),
        wait=wait_exponential(multiplier=multiplier, min=initial_wait, max=max_wait),
        stop=stop_after_attempt(max_attempts),
        reraise=True,
    )


# Pre-built decorator for rate-limit retries (429): up to 5 attempts, max 60 s backoff
retry_on_rate_limit = make_retry_decorator(max_attempts=5, max_wait=60.0)

# Pre-built decorator for transient 5xx retries: up to 3 attempts
retry_on_server_error = make_retry_decorator(max_attempts=3, max_wait=30.0)
