"""Shared utilities: retry decorator, known-ID checkpoint helpers, header extractor."""
import asyncio
import base64
import functools
import random
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Set, Type, Union

from exceptions import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorRateLimitError,
)

_NON_RETRYABLE = (ConnectorAuthError, ConnectorPermissionError, ConnectorNotFoundError)


def retry(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    multiplier: float = 2.0,
    jitter_factor: float = 0.1,
):
    """Async retry decorator with exponential backoff and jitter.

    Retries on ConnectorRateLimitError and generic ConnectorError.
    Does NOT retry on auth / permission / not-found errors.
    """
    def decorator(func):  # type: ignore[no-untyped-def]
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = initial_delay
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except _NON_RETRYABLE:
                    raise
                except (ConnectorRateLimitError, ConnectorError) as exc:
                    if attempt == max_attempts - 1:
                        raise
                    jitter = random.uniform(0, jitter_factor * delay)
                    await asyncio.sleep(delay + jitter)
                    delay *= multiplier
        return wrapper
    return decorator


def load_known_ids(config: Dict[str, Any]) -> Set[str]:
    """Return the set of message IDs previously ingested, stored in config."""
    return set(config.get("known_message_ids", []))


def save_known_ids(config: Dict[str, Any], ids: Set[str]) -> Dict[str, Any]:
    """Return an updated config dict with *ids* persisted as known_message_ids."""
    return {**config, "known_message_ids": list(ids)}


def build_mime_raw(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
) -> str:
    """Build an RFC 2822 MIME message and return a base64url-encoded string (no padding).

    Gmail API requires the padding chars to be stripped — padded base64 causes 400.
    Uses MIMEMultipart when cc or bcc is provided so all headers are set correctly.
    """
    msg: Union[MIMEText, MIMEMultipart]
    if cc or bcc:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain"))
    else:
        msg = MIMEText(body, "plain")

    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc

    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8").rstrip("=")


def extract_header(headers: List[Dict[str, str]], name: str) -> str:
    """Case-insensitive lookup of a Gmail message header value."""
    for header in headers or []:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""
