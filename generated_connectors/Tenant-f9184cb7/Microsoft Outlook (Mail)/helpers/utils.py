"""Retry helper + recipient/message payload builders for Microsoft Graph mail."""
from __future__ import annotations

import asyncio
import random
from typing import Any, Callable, Coroutine, Dict, Iterable, List, Optional

import httpx
import structlog

from exceptions import OutlookMailNetworkError, OutlookMailRateLimitError

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

    Retries on :class:`OutlookMailRateLimitError` (honouring the server-supplied
    ``retry_after`` on the first attempt), :class:`OutlookMailNetworkError`, and
    httpx ``RequestError``. All other exceptions surface immediately.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except OutlookMailRateLimitError as exc:
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
                "outlook_mail.rate_limit — retrying",
                attempt=attempt + 1, delay=delay,
            )
            await asyncio.sleep(delay)
        except (OutlookMailNetworkError, httpx.RequestError) as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "outlook_mail.transient_error — retrying",
                attempt=attempt + 1, delay=delay, error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def to_recipients(addresses: Optional[Iterable[str]]) -> List[Dict[str, Any]]:
    """Build a Microsoft Graph ``toRecipients`` array from plain addresses."""
    if not addresses:
        return []
    return [{"emailAddress": {"address": a}} for a in addresses if a]


def build_message_payload(
    *,
    to: Iterable[str],
    subject: str,
    body: str,
    body_type: str = "HTML",
    cc: Optional[Iterable[str]] = None,
    bcc: Optional[Iterable[str]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the Microsoft Graph ``message`` resource shape.

    ``body_type`` is normalised to one of ``HTML`` / ``Text`` (Graph rejects
    other values).  ``attachments`` is passed through unchanged so callers may
    supply ``fileAttachment`` / ``itemAttachment`` shapes verbatim.
    """
    content_type = body_type.upper() if body_type else "HTML"
    if content_type not in ("HTML", "TEXT"):
        content_type = "HTML"

    message: Dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": content_type.capitalize(), "content": body},
        "toRecipients": to_recipients(to),
    }
    cc_list = to_recipients(cc)
    bcc_list = to_recipients(bcc)
    if cc_list:
        message["ccRecipients"] = cc_list
    if bcc_list:
        message["bccRecipients"] = bcc_list
    if attachments:
        message["attachments"] = attachments
    return message


def build_send_mail_payload(
    *,
    to: Iterable[str],
    subject: str,
    body: str,
    body_type: str = "HTML",
    cc: Optional[Iterable[str]] = None,
    bcc: Optional[Iterable[str]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    save_to_sent_items: bool = True,
) -> Dict[str, Any]:
    """Wrap :func:`build_message_payload` in the /me/sendMail envelope."""
    return {
        "message": build_message_payload(
            to=to, subject=subject, body=body, body_type=body_type,
            cc=cc, bcc=bcc, attachments=attachments,
        ),
        "saveToSentItems": save_to_sent_items,
    }
