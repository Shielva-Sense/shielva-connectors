"""Shared utilities: retry logic, MIME construction, base64url encoding."""
import asyncio
import base64
import random
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any, Callable, Coroutine, Optional

import structlog

from exceptions import GmailRateLimitError

logger = structlog.get_logger(__name__)

# OCP: retry constants — change here, nowhere else
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> Any:
    """Execute *coro_fn()* with exponential-backoff retry.

    Retries on GmailRateLimitError and aiohttp.ClientError subclasses.
    Raises the last exception after exhausting all retries.
    """
    import aiohttp

    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except GmailRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = retry_after if (retry_after and attempt == 0) else min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5), max_delay
            )
            logger.warning(
                "gmail.rate_limit — retrying",
                attempt=attempt + 1,
                delay=delay,
            )
            await asyncio.sleep(delay)
        except aiohttp.ClientError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5), max_delay)
            logger.warning(
                "gmail.client_error — retrying",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def build_mime_message(
    to: str,
    subject: str,
    body: str,
    from_addr: str = "me",
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
) -> MIMEMultipart:
    """Build an RFC 2822 MIME message ready for Gmail send/draft."""
    msg = MIMEMultipart()
    msg["To"] = to
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg


def base64url_encode(raw_bytes: bytes) -> str:
    """Base64url-encode bytes and strip padding — required by Gmail API."""
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")
