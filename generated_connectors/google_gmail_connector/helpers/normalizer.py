"""Maps raw Gmail API message dicts to NormalizedDocument."""
from typing import Any, Dict, List

import structlog
from shared.base_connector import NormalizedDocument

from helpers.utils import extract_header, parse_gmail_date, truncate_preview

logger = structlog.get_logger(__name__)


def normalize(raw_message: Dict[str, Any], tenant_id: str, connector_id: str) -> NormalizedDocument:
    """Convert a raw Gmail message dict to a NormalizedDocument.

    Args:
        raw_message: Dict returned by execute_get_message() with format='metadata'.
        tenant_id:   Tenant identifier for multi-tenant ID namespacing.
        connector_id: Connector instance identifier.

    Returns:
        NormalizedDocument ready for ingestion.
    """
    message_id: str = raw_message.get("id", "")
    thread_id: str = raw_message.get("threadId", "")
    snippet: str = raw_message.get("snippet", "")

    headers: List[Dict[str, str]] = (
        raw_message.get("payload", {}).get("headers", [])
    )

    subject = extract_header(headers, "Subject") or "(no subject)"
    sender = extract_header(headers, "From") or ""
    date_str = extract_header(headers, "Date")
    parsed_date = parse_gmail_date(date_str)

    source_url = f"https://mail.google.com/mail/u/0/#inbox/{message_id}"

    doc_id = f"{tenant_id}_{message_id}"

    metadata: Dict[str, Any] = {
        "sender": sender,
        "thread_id": thread_id,
        "source_url": source_url,
        "labels": raw_message.get("labelIds", []),
    }
    if parsed_date is not None:
        metadata["date"] = parsed_date.isoformat()

    return NormalizedDocument(
        id=doc_id,
        source_id=message_id,
        title=subject,
        content=truncate_preview(snippet, max_chars=200),
        content_type="text",
        source_url=source_url,
        author=sender,
        created_at=parsed_date,
        metadata=metadata,
        source="google_gmail_connector",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_batch(
    raw_messages: List[Dict[str, Any]],
    tenant_id: str,
    connector_id: str,
) -> List[NormalizedDocument]:
    """Normalize a list of raw Gmail messages, skipping any that fail."""
    docs: List[NormalizedDocument] = []
    for msg in raw_messages:
        try:
            docs.append(normalize(msg, tenant_id, connector_id))
        except Exception as exc:
            logger.warning(
                "gmail.normalizer.skip",
                message_id=msg.get("id"),
                error=str(exc),
            )
    return docs
