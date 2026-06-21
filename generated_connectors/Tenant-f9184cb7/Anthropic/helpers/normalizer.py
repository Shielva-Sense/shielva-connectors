"""Normalize Anthropic API responses into NormalizedDocument.

The Anthropic Messages API is a request/response inference surface — it
does not host a crawlable corpus. ``normalize_message_response`` is provided
for callers that want to ingest individual chat completions into a KB (for
example, logging assistant outputs for audit / training-data curation).
"""
from datetime import datetime, timezone
from typing import Any, Dict


def _extract_text(content: Any) -> str:
    """Concatenate the ``text`` blocks of a Messages API ``content`` array."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def normalize_message_response(
    raw: Dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
):
    """Turn an Anthropic ``POST /messages`` response into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    response = raw or {}
    source_id = response.get("id", "")
    model = response.get("model", "")
    usage = response.get("usage", {}) or {}
    now = datetime.now(timezone.utc)

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}" if tenant_id else source_id,
        source_id=source_id,
        title=f"Claude completion {model}" if model else "Claude completion",
        content=_extract_text(response.get("content")),
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=now,
        updated_at=now,
        metadata={
            "model": model,
            "stop_reason": response.get("stop_reason", ""),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "kind": "anthropic.messages",
        },
    )
