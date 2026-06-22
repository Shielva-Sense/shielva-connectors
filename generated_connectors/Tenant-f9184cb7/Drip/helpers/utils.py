"""Utility helpers for the Drip connector.

- ``build_basic_auth_header`` builds Drip's HTTP Basic value:
  ``Basic base64(api_token + ":")`` (token = username, empty password).
- ``encode_subscriber_id`` URL-encodes a Drip identifier (id OR email) for use
  in a URI segment — Drip accepts either form on the same path slot.
- ``with_retry`` retries an async callable on transient errors (429 / 5xx
  surfaced as ``DripServerError`` / ``DripNetworkError`` / ``DripRateLimitError``).
- ``safe_get`` walks a nested dict path without raising.
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import quote

from exceptions import (
    DripError,
    DripNetworkError,
    DripRateLimitError,
    DripServerError,
)

T = TypeVar("T")

_RETRY_STATUS = {429, 500, 502, 503, 504}


def build_basic_auth_header(api_token: str) -> str:
    """Return the value for the Authorization header: ``Basic <base64>``.

    Drip uses HTTP Basic with the api_token as username and an empty password.
    """
    raw = f"{api_token}:".encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"Basic {encoded}"


def encode_subscriber_id(id_or_email: str) -> str:
    """URL-encode an identifier (id or email) for use in a Drip URI segment.

    ``safe=""`` → also encode '@' and '+', which Drip requires for email-shaped
    identifiers on the same URL segment as numeric ids.
    """
    return quote(id_or_email, safe="")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.05,
) -> T:
    """Retry an async callable with exponential backoff on transient errors.

    Retries on ``DripServerError`` / ``DripNetworkError`` / ``DripRateLimitError``
    and any ``DripError`` whose ``status_code`` is in {429, 500, 502, 503, 504}.
    Non-retryable errors (401, 404, etc.) propagate immediately on the first
    call so callers can classify them.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except DripRateLimitError as exc:
            last_exc = exc
        except (DripServerError, DripNetworkError) as exc:
            last_exc = exc
        except DripError as exc:
            if exc.status_code not in _RETRY_STATUS:
                raise
            last_exc = exc

        if attempt >= max_retries:
            break
        await asyncio.sleep(base_delay * (2 ** attempt))

    assert last_exc is not None  # for type-checkers
    raise last_exc


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely without raising on missing intermediate keys."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
