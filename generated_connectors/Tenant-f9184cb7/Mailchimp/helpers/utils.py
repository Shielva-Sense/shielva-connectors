"""Mailchimp connector — normalization and utility helpers."""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable, Dict, Optional

from models import ConnectorDocument


def get_subscriber_hash(email: str) -> str:
    """Return the MD5 hash of the lower-cased, stripped email address.

    This is Mailchimp's canonical subscriber_hash format used to identify a
    member within a list: GET /lists/{list_id}/members/{subscriber_hash}.
    """
    return hashlib.md5(email.lower().strip().encode()).hexdigest()


def extract_dc_from_api_key(api_key: str) -> str:
    """Extract data-center suffix from a Mailchimp API key.

    Mailchimp API keys end with '-<dc>', e.g. 'abc123-us10' → 'us10'.
    Returns an empty string if the key does not contain a '-'.
    """
    if not api_key or "-" not in api_key:
        return ""
    return api_key.rsplit("-", 1)[-1]


def normalize_member(
    member: Dict[str, Any],
    list_id: str,
    list_name: str,
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Mailchimp member object into a ConnectorDocument.

    Stable document id: SHA-256(list_id + ':' + email)[:16].
    """
    email = (member.get("email_address") or "").lower()
    stable_id = hashlib.sha256(
        f"{list_id}:{email}".encode()
    ).hexdigest()[:16]

    full_name = (member.get("full_name") or "").strip()
    first_name = member.get("merge_fields", {}).get("FNAME", "")
    last_name = member.get("merge_fields", {}).get("LNAME", "")
    if not full_name and (first_name or last_name):
        full_name = f"{first_name} {last_name}".strip()

    status = member.get("status", "")
    unique_email_id = member.get("unique_email_id", "")
    web_id = member.get("web_id", "")
    tags = [t.get("name", "") for t in (member.get("tags") or [])]
    timestamp_signup = member.get("timestamp_signup", "")
    timestamp_opt = member.get("timestamp_opt", "")
    last_changed = member.get("last_changed", "")
    language = member.get("language", "")
    vip = member.get("vip", False)
    location = member.get("location", {})
    stats = member.get("stats", {})

    # Human-readable title
    title = email
    if full_name:
        title = f"{full_name} <{email}>"

    # Structured content block
    content_parts: list[str] = [
        f"Audience: {list_name}",
        f"Email: {email}",
    ]
    if full_name:
        content_parts.append(f"Name: {full_name}")
    content_parts.append(f"Status: {status}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    if language:
        content_parts.append(f"Language: {language}")
    if timestamp_signup:
        content_parts.append(f"Signup: {timestamp_signup}")
    if last_changed:
        content_parts.append(f"Last changed: {last_changed}")

    metadata: Dict[str, Any] = {
        "list_id": list_id,
        "list_name": list_name,
        "email": email,
        "full_name": full_name,
        "status": status,
        "unique_email_id": unique_email_id,
        "web_id": web_id,
        "tags": tags,
        "timestamp_signup": timestamp_signup,
        "timestamp_opt": timestamp_opt,
        "last_changed": last_changed,
        "language": language,
        "vip": vip,
        "location": location,
        "stats": stats,
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "mailchimp",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="email_contact",
        metadata=metadata,
    )


def normalize_campaign(
    campaign: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Mailchimp campaign object into a ConnectorDocument.

    Stable document id: SHA-256('campaign:' + campaign_id)[:16].
    """
    campaign_id = campaign.get("id", "")
    stable_id = hashlib.sha256(
        f"campaign:{campaign_id}".encode()
    ).hexdigest()[:16]

    settings = campaign.get("settings", {})
    subject_line = settings.get("subject_line", "")
    title = settings.get("title", "") or subject_line or campaign_id
    from_name = settings.get("from_name", "")
    reply_to = settings.get("reply_to", "")

    campaign_type = campaign.get("type", "")
    status = campaign.get("status", "")
    send_time = campaign.get("send_time", "")
    create_time = campaign.get("create_time", "")
    emails_sent = campaign.get("emails_sent", 0)
    recipients = campaign.get("recipients", {})
    list_id = recipients.get("list_id", "")
    list_name = recipients.get("list_name", "")

    content_parts: list[str] = [
        f"Campaign: {title}",
        f"Type: {campaign_type}",
        f"Status: {status}",
    ]
    if subject_line:
        content_parts.append(f"Subject: {subject_line}")
    if from_name:
        content_parts.append(f"From: {from_name}")
    if list_name:
        content_parts.append(f"Audience: {list_name}")
    if send_time:
        content_parts.append(f"Sent: {send_time}")
    elif create_time:
        content_parts.append(f"Created: {create_time}")
    if emails_sent:
        content_parts.append(f"Emails sent: {emails_sent}")

    metadata: Dict[str, Any] = {
        "campaign_id": campaign_id,
        "type": campaign_type,
        "status": status,
        "subject_line": subject_line,
        "title": title,
        "from_name": from_name,
        "reply_to": reply_to,
        "send_time": send_time,
        "create_time": create_time,
        "emails_sent": emails_sent,
        "list_id": list_id,
        "list_name": list_name,
        "connector_id": connector_id,
        "tenant_id": tenant_id,
        "source": "mailchimp",
    }

    return ConnectorDocument(
        id=stable_id,
        title=title,
        content="\n".join(content_parts),
        type="email_campaign",
        metadata=metadata,
    )


async def with_retry(
    fn: Callable,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute an async callable with exponential-backoff retry.

    Skips retry on MailchimpAuthError — re-authorizing is required.
    """
    from exceptions import MailchimpAuthError, MailchimpError

    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            result = fn(*args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except MailchimpAuthError:
            raise
        except MailchimpError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
