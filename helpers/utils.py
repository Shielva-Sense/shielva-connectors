"""
Gmail Connector — Shared Utilities
SRP-B: All MIME construction, email validation, attachment sizing, and
base64url encoding live here. connector.py NEVER encodes or validates inline.
"""
import base64
import re
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from exceptions import GmailAttachmentError, GmailValidationError

# RFC 5322 simplified regex (covers the vast majority of valid addresses)
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)

MAX_ATTACHMENT_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB


def validate_email_address(email: str) -> None:
    """
    Validate an email address against RFC 5322 regex.
    Raises GmailValidationError on failure.
    """
    # Handle "Name <addr>" format
    match = re.search(r"<(.+?)>", email)
    addr = match.group(1) if match else email.strip()
    if not _EMAIL_RE.match(addr):
        raise GmailValidationError(f"Invalid email address format: {email!r}")


def calculate_attachment_size(attachments: Optional[List[Dict[str, Any]]]) -> int:
    """
    Sum total byte size of all attachments.
    Raises GmailAttachmentError if total exceeds MAX_ATTACHMENT_SIZE_BYTES (25 MB).
    Returns total size in bytes.
    """
    if not attachments:
        return 0
    total = sum(len(att.get("data", b"")) for att in attachments)
    if total > MAX_ATTACHMENT_SIZE_BYTES:
        raise GmailAttachmentError(
            f"Total attachment size {total} bytes exceeds 25 MB limit"
        )
    return total


def build_raw_email_message(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    reply_to: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Construct an RFC 2822 MIME message and return a base64url-encoded string
    suitable for the Gmail API ``raw`` field.

    Each attachment dict MUST have:
      - "filename": str
      - "data": bytes
      - "mimetype": str  (e.g. "application/pdf")
    """
    if attachments:
        msg: Any = MIMEMultipart()
        msg.attach(MIMEText(body, "plain"))
        for att in attachments:
            part = MIMEApplication(att["data"], Name=att["filename"])
            part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
            part["Content-Type"] = att.get("mimetype", "application/octet-stream")
            msg.attach(part)
    else:
        msg = MIMEText(body, "plain")

    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    if reply_to:
        msg["Reply-To"] = reply_to

    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("utf-8")


def epoch_from_datetime(dt: Any) -> int:
    """Convert a datetime object to a Unix epoch integer (for Gmail 'after:' queries)."""
    from datetime import datetime, timezone
    if isinstance(dt, (int, float)):
        return int(dt)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    raise ValueError(f"Cannot convert {type(dt)} to epoch")
