"""Misc utility helpers for the Insightly connector.

- `build_basic_auth_header` — Insightly HTTP Basic header (`api_key:` base64'd)
- `with_retry`              — coarse exponential-backoff retry around any async coro
- `safe_get`                — nested-dict walker
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


def build_basic_auth_header(api_key: str) -> str:
    """Build the `Authorization: Basic …` value for the Insightly API.

    Insightly accepts the API key as the HTTP Basic *username* with an empty
    *password* — i.e. `base64(api_key + ":")`. Returns the full header value
    including the leading `Basic ` prefix.
    """
    if not api_key:
        raise ValueError("api_key is required to build the Basic auth header")
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx and transport errors; this helper
    is the connector-layer escape hatch for unexpected transient errors that
    leak past the client (e.g. JSON-decode flakiness behind misbehaving
    proxies).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


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
