from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import WhatsAppAuthError, WhatsAppError, WhatsAppRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: WhatsAppError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except WhatsAppAuthError:
            # Do not retry auth failures — token is invalid
            raise
        except WhatsAppRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except WhatsAppError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _render_components(components: list[dict[str, Any]]) -> str:
    """Render template components to human-readable text."""
    parts: list[str] = []
    for comp in components:
        comp_type = comp.get("type", "").upper()
        text = comp.get("text", "")
        if comp_type == "HEADER":
            format_ = comp.get("format", "TEXT")
            if format_ == "TEXT" and text:
                parts.append(f"HEADER: {text}")
            else:
                parts.append(f"HEADER: [{format_}]")
        elif comp_type == "BODY" and text:
            parts.append(f"BODY: {text}")
        elif comp_type == "FOOTER" and text:
            parts.append(f"FOOTER: {text}")
        elif comp_type == "BUTTONS":
            buttons: list[dict[str, Any]] = comp.get("buttons", [])
            for btn in buttons:
                btn_type = btn.get("type", "")
                btn_text = btn.get("text", "")
                parts.append(f"BUTTON [{btn_type}]: {btn_text}")
    return "\n".join(parts) if parts else "(no components)"


def normalize_template(
    template: dict[str, Any],
    connector_id: str,
    tenant_id: str,
    waba_id: str,
) -> ConnectorDocument:
    """Convert a raw Meta message template into a ConnectorDocument.

    The source_id is a 16-character SHA-256 prefix of the template ID
    to produce a stable, compact identifier.
    """
    template_id: str = str(template.get("id", ""))
    name: str = template.get("name", "unknown")
    status: str = template.get("status", "")
    category: str = template.get("category", "")
    language: str = template.get("language", "")
    components: list[dict[str, Any]] = template.get("components", [])

    # Stable 16-char ID derived from the Meta template ID
    source_id = hashlib.sha256(template_id.encode()).hexdigest()[:16]

    title = f"Template: {name} ({language})"
    content = _render_components(components)

    return ConnectorDocument(
        source_id=source_id,
        title=title,
        content=content,
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://business.facebook.com/wa/manage/message-templates/?waba_id={waba_id}",
        metadata={
            "template_id": template_id,
            "name": name,
            "status": status,
            "category": category,
            "language": language,
            "waba_id": waba_id,
        },
    )
