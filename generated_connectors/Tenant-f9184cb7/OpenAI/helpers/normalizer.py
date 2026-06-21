"""Map raw OpenAI API payloads → NormalizedDocument.

Multi-tenant: every NormalizedDocument id has the form
`{tenant_id}_{source_id}` so two tenants with the same OpenAI IDs
produce distinct documents.

OpenAI is an LLM provider — there is no canonical document corpus to sync.
The default connector `sync()` is a no-op; these helpers are provided so
downstream callers (e.g. transcript logging) can normalise chat completions
or transcriptions into the gateway's `NormalizedDocument` shape if they need
to.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from shared.base_connector import NormalizedDocument


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_chat_completion(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> NormalizedDocument:
    """Map a `/v1/chat/completions` response to a NormalizedDocument."""
    src_id = str(raw.get("id") or "chat-completion")
    choices = raw.get("choices") or []
    first = choices[0] if choices else {}
    content = ((first.get("message") or {}).get("content") or "") if isinstance(first, dict) else ""
    model = raw.get("model") or ""
    created_ts = raw.get("created")
    created = (
        datetime.fromtimestamp(int(created_ts), tz=timezone.utc)
        if isinstance(created_ts, (int, float))
        else _now_utc()
    )
    return NormalizedDocument(
        id=f"{tenant_id}_{src_id}",
        source_id=src_id,
        title=f"Chat completion {src_id}",
        content=content,
        content_type="text/plain",
        source="openai.chat",
        created_at=created,
        updated_at=created,
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "model": model,
            "finish_reason": first.get("finish_reason") if isinstance(first, dict) else None,
            "usage": raw.get("usage") or {},
        },
    )


def normalize_transcription(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
    file_name: str = "",
) -> NormalizedDocument:
    """Map a `/v1/audio/transcriptions` response to a NormalizedDocument."""
    src_id = str(raw.get("id") or file_name or "transcription")
    text = raw.get("text") or ""
    return NormalizedDocument(
        id=f"{tenant_id}_{src_id}",
        source_id=src_id,
        title=f"Transcription {src_id}",
        content=text,
        content_type="text/plain",
        source="openai.audio",
        created_at=_now_utc(),
        updated_at=_now_utc(),
        tenant_id=tenant_id,
        connector_id=connector_id,
        metadata={
            "language": raw.get("language"),
            "duration": raw.get("duration"),
            "file_name": file_name,
        },
    )


def normalize_files_listing(
    raw: Dict[str, Any],
    *,
    tenant_id: str,
    connector_id: str,
) -> List[NormalizedDocument]:
    """Map a `/v1/files` list response to NormalizedDocuments (one per file)."""
    docs: List[NormalizedDocument] = []
    for item in raw.get("data") or []:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or "")
        if not fid:
            continue
        created_ts = item.get("created_at")
        created = (
            datetime.fromtimestamp(int(created_ts), tz=timezone.utc)
            if isinstance(created_ts, (int, float))
            else _now_utc()
        )
        docs.append(
            NormalizedDocument(
                id=f"{tenant_id}_{fid}",
                source_id=fid,
                title=str(item.get("filename") or fid),
                content="",
                content_type="application/octet-stream",
                source="openai.files",
                created_at=created,
                updated_at=created,
                tenant_id=tenant_id,
                connector_id=connector_id,
                metadata={
                    "purpose": item.get("purpose"),
                    "bytes": item.get("bytes"),
                    "status": item.get("status"),
                },
            )
        )
    return docs
