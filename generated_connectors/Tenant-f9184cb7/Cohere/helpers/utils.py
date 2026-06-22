"""Misc utility helpers for the Cohere connector."""
import asyncio
from typing import Any, Awaitable, Callable, Dict, TypeVar

T = TypeVar("T")


def mask_api_key(api_key: str) -> str:
    """Return a masked form of an API key for safe logging."""
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}…{api_key[-4:]}"


def summarize_chat_response(response: Dict[str, Any]) -> str:
    """Extract the assistant text from a Cohere /chat response, if present.

    The v2 chat response shape:
        { "message": { "content": [ { "type": "text", "text": "..." } ] }, ... }
    """
    if not isinstance(response, dict):
        return ""
    message = response.get("message", {})
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    if isinstance(content, str):
        return content
    return ""


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx; this helper retries unexpected
    transient errors that escape the client (e.g. JSON decode flakiness on
    intermittent proxies).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — retry-everything is the point
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")
