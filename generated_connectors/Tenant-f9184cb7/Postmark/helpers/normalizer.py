"""Transforms raw Postmark API responses into ``NormalizedDocument`` objects.

Postmark outbound messages have a stable shape:

  { MessageID, To: [{Email, Name}], From, Subject, Body{HTML,Text} | HtmlBody | TextBody,
    Tag, MessageStream, ReceivedAt | SubmittedAt, Status, MessageEvents: [...] }

We map this to a Shielva ``NormalizedDocument`` so the same KB-ingest path used
by other connectors works unchanged.

The document ``id`` is tenant-scoped — ``f"{tenant_id}_{source_id}"`` — so
cross-tenant ID collisions are impossible.
"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import structlog
from shared.base_connector import NormalizedDocument

logger = structlog.get_logger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    return _HTML_TAG_RE.sub("", html or "").strip()


def _parse_postmark_ts(value: Any) -> Optional[datetime]:
    """Postmark timestamps are ISO 8601 with trailing 'Z' — normalize to UTC."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _format_recipients(recipients: Any) -> str:
    """Postmark returns To/Cc/Bcc as either a string or a list of {Email,Name}."""
    if isinstance(recipients, str):
        return recipients
    if isinstance(recipients, list):
        return ", ".join(
            r.get("Email", "") if isinstance(r, dict) else str(r) for r in recipients
        )
    return ""


def normalize_message(
    message: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert a Postmark outbound-message detail object to a NormalizedDocument."""
    message_id = str(message.get("MessageID", "") or message.get("messageid", ""))
    subject = message.get("Subject", "") or "(no subject)"
    from_addr = message.get("From", "") or message.get("FromEmail", "") or ""
    to_addr = _format_recipients(message.get("To", []))
    cc_addr = _format_recipients(message.get("Cc", []))
    bcc_addr = _format_recipients(message.get("Bcc", []))
    tag = message.get("Tag", "") or ""
    status = message.get("Status", "") or ""
    stream = message.get("MessageStream", "") or "outbound"

    body_envelope = message.get("Body") if isinstance(message.get("Body"), dict) else {}
    html_body = message.get("HtmlBody", "") or body_envelope.get("HTML", "") or ""
    text_body = message.get("TextBody", "") or body_envelope.get("Text", "") or ""
    content = text_body or _strip_html(html_body) or subject

    received_at = _parse_postmark_ts(
        message.get("ReceivedAt", "") or message.get("SubmittedAt", "")
    )

    return NormalizedDocument(
        id=f"{tenant_id}_{message_id}",
        source_id=message_id,
        title=subject,
        content=content,
        content_type="text",
        source_url=f"https://account.postmarkapp.com/servers/messages/{message_id}",
        author=from_addr,
        created_at=received_at,
        updated_at=received_at,
        metadata={
            "to": to_addr,
            "cc": cc_addr,
            "bcc": bcc_addr,
            "tag": tag,
            "status": status,
            "message_stream": stream,
            "html_body": html_body,
            "events": message.get("MessageEvents", []),
            "kind": "postmark.outbound_message",
            "connector_id": connector_id,
            "tenant_id": tenant_id,
        },
    )


def normalize_inbound_message(
    message: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> NormalizedDocument:
    """Convert a Postmark inbound-message detail object to a NormalizedDocument."""
    message_id = str(message.get("MessageID", "") or message.get("messageid", ""))
    subject = message.get("Subject", "") or "(no subject)"
    from_addr = message.get("From", "") or message.get("FromEmail", "") or ""
    to_addr = _format_recipients(message.get("To", []))
    mailbox_hash = message.get("MailboxHash", "") or ""
    original_recipient = message.get("OriginalRecipient", "") or ""

    body_envelope = message.get("Body") if isinstance(message.get("Body"), dict) else {}
    html_body = message.get("HtmlBody", "") or body_envelope.get("HTML", "") or ""
    text_body = message.get("TextBody", "") or body_envelope.get("Text", "") or ""
    content = text_body or _strip_html(html_body) or subject

    received_at = _parse_postmark_ts(message.get("ReceivedAt", ""))

    return NormalizedDocument(
        id=f"{tenant_id}_{message_id}",
        source_id=message_id,
        title=subject,
        content=content,
        content_type="text",
        source_url=f"https://account.postmarkapp.com/servers/messages/inbound/{message_id}",
        author=from_addr,
        created_at=received_at,
        updated_at=received_at,
        metadata={
            "to": to_addr,
            "mailbox_hash": mailbox_hash,
            "original_recipient": original_recipient,
            "html_body": html_body,
            "status": message.get("Status", ""),
            "kind": "postmark.inbound_message",
            "connector_id": connector_id,
            "tenant_id": tenant_id,
        },
    )
